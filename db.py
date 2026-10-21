"""In-memory copy of the content tables, loaded at app start and on "Refresh Data".

Tables are read (SELECT only, read-only session) from the local dump database and copied
into an in-memory DuckDB, so they can be queried with SQL and handled as pandas DataFrames.
"""

import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from urllib.parse import quote_plus

import duckdb
import paramiko
import pandas as pd
import streamlit as st
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

# sshtunnel still references paramiko.DSSKey, which newer paramiko releases removed
if not hasattr(paramiko, "DSSKey"):
    paramiko.DSSKey = None

from sshtunnel import SSHTunnelForwarder  # noqa: E402

# Only the tables the app queries. Add a table here when a new query needs it.
TABLE_NAMES = [
    "character",
    "season_variant",  # season titles
    "season_character",  # MAIN characters of a season
    "episode",
    "episode_variant",  # episode titles
    "audio_research",
    "audio_research_character",
    "chapter",
    "mantra",
    "user_character_preference",
    "ott_user_info",  # looks users up by email / phone
    "user_metadata",  # preferred language per user
]

# Tables where only some columns are wanted. ott_user_info also holds password_hash and
# other personal data, which this app must never read or copy.
TABLE_COLUMNS = {"ott_user_info": ["user_id", "email", "phone_number"], "user_metadata": ["id", "language"]}


def _select_sql(table: str) -> str:
    columns = ", ".join(f'"{c}"' for c in TABLE_COLUMNS[table]) if table in TABLE_COLUMNS else "*"
    return f'SELECT {columns} FROM "{table}"'

LOCAL_DB_URL = os.getenv(
    "LOCAL_DB_URL",
    "postgresql+psycopg2://dev_user:password@localhost:5432/main_character_dev",
)


@dataclass
class Tables:
    con: duckdb.DuckDBPyConnection
    loaded_at: float  # when the app read the dump
    data_as_of: pd.Timestamp | None  # newest `updated_at` found in the dump


@st.cache_resource(show_spinner="Loading latest dump...")
def load_tables() -> Tables:
    """Copy every table in TABLE_NAMES from the local dump into an in-memory DuckDB."""
    engine = create_engine(
        LOCAL_DB_URL,
        connect_args={
            "connect_timeout": 5,
            # Postgres itself rejects any write on this connection
            "options": "-c default_transaction_read_only=on",
        },
    )
    con = duckdb.connect(":memory:")
    newest = None
    try:
        for table in TABLE_NAMES:
            df = pd.read_sql(_select_sql(table), engine)
            if "updated_at" in df.columns and df["updated_at"].notna().any():
                latest = df["updated_at"].max()
                newest = latest if newest is None else max(newest, latest)
            # a real table (not a registered view) so it is visible to every cursor
            con.register("_loading", df)
            con.execute(f'CREATE TABLE "{table}" AS SELECT * FROM _loading')
            con.unregister("_loading")
    finally:
        engine.dispose()
    return Tables(con=con, loaded_at=time.time(), data_as_of=newest)


def _require_env(*names: str) -> None:
    missing = [k for k in names if not os.getenv(k)]
    if missing:
        raise RuntimeError(f"Missing in .env: {', '.join(missing)}")


@contextmanager
def _prod_engine():
    """Read-only SQLAlchemy engine on prod Postgres, through a short-lived SSH tunnel."""
    _require_env("SSH_HOST", "SSH_USER", "SSH_KEY_PATH", "DB_HOST", "DB_USER", "DB_PASSWORD", "DB_NAME")
    server = SSHTunnelForwarder(
        (os.environ["SSH_HOST"], 22),
        ssh_username=os.environ["SSH_USER"],
        ssh_pkey=os.path.expanduser(os.environ["SSH_KEY_PATH"]),
        remote_bind_address=(os.environ["DB_HOST"], int(os.getenv("DB_PORT", "5432"))),
        local_bind_address=("127.0.0.1", 0),  # auto-pick a free port
        set_keepalive=30,
    )
    engine = None
    try:
        server.start()
        engine = create_engine(
            f"postgresql+psycopg2://{os.environ['DB_USER']}:{quote_plus(os.environ['DB_PASSWORD'])}"
            f"@127.0.0.1:{server.local_bind_port}/{os.environ['DB_NAME']}",
            connect_args={
                "connect_timeout": 10,
                # Postgres itself rejects any write on this connection
                "options": "-c default_transaction_read_only=on",
            },
            pool_pre_ping=True,
        )
        yield engine
    finally:
        if engine is not None:
            engine.dispose()
        server.stop()


def _copy_tables(source_engine, dest_engine, on_progress=None, tables=None) -> dict[str, int]:
    """Copy the given tables (default: all of TABLE_NAMES) from source to dest, replacing the dest tables.

    Everything is read from the source first, and the dest tables are replaced in a single
    transaction, so a failure at any point leaves the dest tables as they were.
    """
    # on_progress(message, fraction 0-1): reading the tables is 90% of the bar
    tables = list(tables) if tables is not None else TABLE_NAMES
    frames = {}
    for i, table in enumerate(tables):
        if on_progress:
            on_progress(f"Reading {table} ({i + 1}/{len(tables)})...", 0.9 * i / len(tables))
        frames[table] = pd.read_sql(_select_sql(table), source_engine)
    if on_progress:
        on_progress("Replacing local tables...", 0.9)
    with dest_engine.begin() as conn:
        for table, df in frames.items():
            df.to_sql(table, conn, if_exists="replace", index=False)
    if on_progress:
        on_progress("Done", 1.0)
    return {table: len(df) for table, df in frames.items()}


def pull_latest_dump(on_progress=None, tables=None) -> dict[str, int]:
    """Refresh the chosen tables (default: all) of the local dump from prod, then drop the in-memory copy.

    Prod is only read (read-only session). Only the local database is written to.
    """
    if tables is not None:
        unknown = [t for t in tables if t not in TABLE_NAMES]
        if unknown:
            raise KeyError(f"Unknown table(s): {', '.join(unknown)}")
        if not tables:
            raise ValueError("Choose at least one table to pull.")
    local = make_url(LOCAL_DB_URL)
    if local.host not in ("localhost", "127.0.0.1"):
        raise RuntimeError(f"Refusing to write: LOCAL_DB_URL must be a local database, not {local.host}")
    dest = create_engine(LOCAL_DB_URL)
    try:
        if on_progress:
            on_progress("Opening SSH tunnel to prod...", 0.0)
        with _prod_engine() as source:
            counts = _copy_tables(source, dest, on_progress, tables)
    finally:
        dest.dispose()
    load_tables.clear()
    return counts


def run_query(sql: str, params: list | None = None) -> pd.DataFrame:
    """Run SQL over the loaded tables and return a DataFrame."""
    # a cursor per call keeps this safe across Streamlit's script threads
    return load_tables().con.cursor().execute(sql, params or []).df()


def get_table(name: str) -> pd.DataFrame:
    """Return a whole loaded table as a DataFrame."""
    if name not in TABLE_NAMES:
        raise KeyError(f"Unknown table: {name}")
    return run_query(f'SELECT * FROM "{name}"')


# MAIN characters, joined into one string per content id.
# Seasons differentiate MAIN characters; audio research and mantras do not, so every
# character linked to them counts as MAIN.
_CHARACTERS_CTE = """
season_names AS (
    SELECT sc.season_id AS id, string_agg(DISTINCT c.name, ', ') AS characters
    FROM season_character sc
    JOIN "character" c ON c.id = sc.character_id
    WHERE sc.character_type = 'MAIN'
      AND COALESCE(sc.is_deleted, FALSE) = FALSE
      AND COALESCE(c.is_deleted, FALSE) = FALSE
    GROUP BY sc.season_id
),
audio_research_names AS (
    SELECT l.id, string_agg(DISTINCT c.name, ', ') AS characters
    FROM (
        SELECT audio_research_id AS id, character_id FROM audio_research_character
        UNION
        SELECT id, character_id FROM audio_research
    ) l
    JOIN "character" c ON c.id = l.character_id
    WHERE COALESCE(c.is_deleted, FALSE) = FALSE
    GROUP BY l.id
),
mantra_names AS (
    SELECT m.id, c.name AS characters
    FROM mantra m
    JOIN "character" c ON c.id = m.character_id
    WHERE COALESCE(c.is_deleted, FALSE) = FALSE
)
"""

# Per content kind: SQL returning (id, title, parent_title, characters) for every item.
# Episodes and chapters carry their parent's title (season/series, audio research) and
# inherit its characters.
_TITLE_SQL = {
    "videos": """
        SELECT sv.season_id AS id, sv.title, NULL AS parent_title, sn.characters
        FROM season_variant sv
        LEFT JOIN season_names sn ON sn.id = sv.season_id
        UNION ALL
        SELECT e.id, ev.title, sv.title AS parent_title, sn.characters
        FROM episode e
        LEFT JOIN (
            SELECT episode_id, title,
                   ROW_NUMBER() OVER (PARTITION BY episode_id ORDER BY updated_at DESC NULLS LAST) AS rn
            FROM episode_variant
        ) ev ON ev.episode_id = e.id AND ev.rn = 1
        LEFT JOIN season_variant sv ON sv.season_id = e.season_id
        LEFT JOIN season_names sn ON sn.id = e.season_id
    """,
    "audios": """
        SELECT ar.id, ar.title, NULL AS parent_title, an.characters
        FROM audio_research ar
        LEFT JOIN audio_research_names an ON an.id = ar.id
        UNION ALL
        SELECT c.id, c.title, ar.title AS parent_title, an.characters
        FROM chapter c
        LEFT JOIN audio_research ar ON ar.id = c.audio_research_id
        LEFT JOIN audio_research_names an ON an.id = c.audio_research_id
    """,
    "mantras": """
        SELECT m.id, m.title, NULL AS parent_title, mn.characters
        FROM mantra m
        LEFT JOIN mantra_names mn ON mn.id = m.id
    """,
}


def get_titles(kind: str, ids: list[str]) -> pd.DataFrame:
    """Return id, title, parent_title, characters for the given ids of one content kind."""
    return run_query(
        f"WITH {_CHARACTERS_CTE} "
        f"SELECT * FROM ({_TITLE_SQL[kind]}) WHERE id IN (SELECT UNNEST(?::VARCHAR[]))",
        [list(ids)],
    )


def get_user_characters(user_id: str) -> list[str]:
    """Names of the characters the user selected in their preferences."""
    df = run_query(
        """
        SELECT DISTINCT c.name
        FROM user_character_preference p
        JOIN "character" c ON c.id = p.character_id
        WHERE p.user_id = ?
          AND COALESCE(p.is_deleted, FALSE) = FALSE
          AND COALESCE(c.is_deleted, FALSE) = FALSE
        ORDER BY c.name
        """,
        [user_id],
    )
    return df["name"].tolist()


def get_user_language(user_id: str) -> str | None:
    """The user's preferred language from user_metadata, or None when not set."""
    df = run_query("SELECT language FROM user_metadata WHERE id = ?", [user_id])
    values = df["language"].dropna().astype(str).str.strip()
    values = values[values != ""]
    return values.iloc[0] if len(values) else None


# ---------------------------------------------------------------- user lookup

@st.cache_data
def country_options() -> dict[str, str]:
    """Label -> dialling prefix, e.g. {'India (+91)': '+91', ...}. India first."""
    import phonenumbers
    import pycountry

    options = {}
    for region in phonenumbers.SUPPORTED_REGIONS:
        country = pycountry.countries.get(alpha_2=region)
        name = country.name if country else region
        prefix = f"+{phonenumbers.country_code_for_region(region)}"
        options[f"{name} ({prefix})"] = prefix
    ordered = dict(sorted(options.items()))
    india = "India (+91)"
    return {india: ordered.pop(india), **ordered}


def build_phone_number(country_prefix: str, number: str) -> str:
    """'+91' + '8910XXX162' -> '+918910XXX162'. Spaces, dashes and brackets are ignored."""
    digits = "".join(ch for ch in number if ch.isdigit())
    if not digits:
        raise ValueError("Enter a phone number.")
    return f"{country_prefix}{digits}"


def find_user_ids(email: str | None = None, phone_number: str | None = None) -> list[str]:
    """User ids matching an email or a full phone number (with country code), not both."""
    if bool(email) == bool(phone_number):
        raise ValueError("Search by either email or phone number, not both.")
    if email:
        df = run_query(
            "SELECT DISTINCT user_id FROM ott_user_info WHERE lower(trim(email)) = lower(trim(?))",
            [email],
        )
    else:
        df = run_query(
            "SELECT DISTINCT user_id FROM ott_user_info WHERE trim(phone_number) = trim(?)",
            [phone_number],
        )
    return df["user_id"].dropna().tolist()
