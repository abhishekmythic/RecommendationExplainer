"""Everything the app reads from production Postgres, per user and per request.

Nothing is copied locally: each lookup queries production (read-only session) for just the
one user and the few content items recommended to them.

Inside the production network (the intended deployment) the app connects straight to DB_HOST.
Set SSH_HOST (with SSH_USER and SSH_KEY_PATH) only to reach the database from outside, through
an SSH tunnel.
"""

import os
import threading
from urllib.parse import quote_plus

import pandas as pd
import streamlit as st
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError


def _require_env(*names: str) -> None:
    missing = [k for k in names if not os.getenv(k)]
    if missing:
        raise RuntimeError(f"Missing environment variables: {', '.join(missing)}")


def _start_tunnel(host: str, port: int):
    """SSH tunnel to host:port; the database is then reachable on 127.0.0.1:<tunnel.local_bind_port>."""
    _require_env("SSH_USER", "SSH_KEY_PATH")
    import paramiko

    # sshtunnel still references paramiko.DSSKey, which newer paramiko releases removed
    if not hasattr(paramiko, "DSSKey"):
        paramiko.DSSKey = None
    from sshtunnel import SSHTunnelForwarder

    server = SSHTunnelForwarder(
        (os.environ["SSH_HOST"], 22),
        ssh_username=os.environ["SSH_USER"],
        ssh_pkey=os.path.expanduser(os.environ["SSH_KEY_PATH"]),
        remote_bind_address=(host, port),
        local_bind_address=("127.0.0.1", 0),  # auto-pick a free port
        set_keepalive=30,
    )
    server.start()
    return server


def _open_prod():
    """Connect to prod Postgres; return (tunnel or None, read-only SQLAlchemy engine)."""
    _require_env("DB_HOST", "DB_USER", "DB_PASSWORD", "DB_NAME")
    host, port = os.environ["DB_HOST"], int(os.getenv("DB_PORT", "5432"))
    server = None
    if os.getenv("SSH_HOST"):
        server = _start_tunnel(host, port)
        host, port = "127.0.0.1", server.local_bind_port
    connect_args = {
        "connect_timeout": 10,
        # Postgres itself rejects any write on this connection
        "options": "-c default_transaction_read_only=on",
    }
    if os.getenv("DB_SSLMODE"):
        connect_args["sslmode"] = os.environ["DB_SSLMODE"]  # e.g. "require" or "verify-full"
    engine = create_engine(
        f"postgresql+psycopg2://{os.environ['DB_USER']}:{quote_plus(os.environ['DB_PASSWORD'])}"
        f"@{host}:{port}/{os.environ['DB_NAME']}",
        connect_args=connect_args,
        pool_recycle=1800,
    )
    return server, engine


# One long-lived engine (and tunnel, if any) for the queries, shared by every Streamlit session.
_user_db: dict = {"server": None, "engine": None}
_user_db_lock = threading.Lock()


def _close_user_db() -> None:
    engine, server = _user_db["engine"], _user_db["server"]
    _user_db["engine"] = _user_db["server"] = None
    if engine is not None:
        engine.dispose()
    if server is not None:
        server.stop()


def _user_engine(reconnect: bool = False):
    with _user_db_lock:
        server = _user_db["server"]
        tunnel_down = server is not None and not server.is_active
        if reconnect or _user_db["engine"] is None or tunnel_down:
            _close_user_db()
            _user_db["server"], _user_db["engine"] = _open_prod()
        return _user_db["engine"]


def run_user_query(sql: str, **params) -> pd.DataFrame:
    """Run a named-parameter SQL (:name) on prod (read-only) and return a DataFrame.

    The connection is opened on first use and kept; one dropped connection is retried on a fresh one.
    """
    for attempt in (0, 1):
        try:
            with _user_engine(reconnect=attempt == 1).connect() as conn:
                return pd.read_sql(text(sql), conn, params=params)
        except OperationalError:
            if attempt == 1:
                raise


# ---------------------------------------------------------------- titles
# Per content kind: SQL returning (id, title, parent_title, characters) for just the :ids asked for.
# Seasons differentiate MAIN characters; audio research and mantras do not, so every character
# linked to them counts as MAIN. Episodes and chapters carry their parent's title (season/series,
# audio research) and inherit its characters.
_TITLE_SQL = {
    "videos": """
        WITH ep AS (SELECT id, season_id FROM episode WHERE id = ANY(:ids)),
        season_names AS (
            SELECT sc.season_id AS id, string_agg(DISTINCT c.name, ', ') AS characters
            FROM season_character sc
            JOIN "character" c ON c.id = sc.character_id
            WHERE sc.character_type = 'MAIN'
              AND COALESCE(sc.is_deleted, FALSE) = FALSE
              AND COALESCE(c.is_deleted, FALSE) = FALSE
              AND (sc.season_id = ANY(:ids) OR sc.season_id IN (SELECT season_id FROM ep))
            GROUP BY sc.season_id
        )
        SELECT sv.season_id AS id, sv.title, NULL::text AS parent_title, sn.characters
        FROM season_variant sv
        LEFT JOIN season_names sn ON sn.id = sv.season_id
        WHERE sv.season_id = ANY(:ids)
        UNION ALL
        SELECT ep.id, ev.title, sv.title AS parent_title, sn.characters
        FROM ep
        LEFT JOIN (
            SELECT DISTINCT ON (episode_id) episode_id, title
            FROM episode_variant
            WHERE episode_id IN (SELECT id FROM ep)
            ORDER BY episode_id, updated_at DESC NULLS LAST
        ) ev ON ev.episode_id = ep.id
        LEFT JOIN season_variant sv ON sv.season_id = ep.season_id
        LEFT JOIN season_names sn ON sn.id = ep.season_id
    """,
    "audios": """
        WITH ch AS (SELECT id, title, audio_research_id FROM chapter WHERE id = ANY(:ids)),
        audio_research_names AS (
            SELECT l.id, string_agg(DISTINCT c.name, ', ') AS characters
            FROM (
                SELECT audio_research_id AS id, character_id FROM audio_research_character
                UNION
                SELECT id, character_id FROM audio_research
            ) l
            JOIN "character" c ON c.id = l.character_id
            WHERE COALESCE(c.is_deleted, FALSE) = FALSE
              AND (l.id = ANY(:ids) OR l.id IN (SELECT audio_research_id FROM ch))
            GROUP BY l.id
        )
        SELECT ar.id, ar.title, NULL::text AS parent_title, an.characters
        FROM audio_research ar
        LEFT JOIN audio_research_names an ON an.id = ar.id
        WHERE ar.id = ANY(:ids)
        UNION ALL
        SELECT ch.id, ch.title, ar.title AS parent_title, an.characters
        FROM ch
        LEFT JOIN audio_research ar ON ar.id = ch.audio_research_id
        LEFT JOIN audio_research_names an ON an.id = ch.audio_research_id
    """,
    "mantras": """
        SELECT m.id, m.title, NULL::text AS parent_title, c.name AS characters
        FROM mantra m
        LEFT JOIN "character" c ON c.id = m.character_id AND COALESCE(c.is_deleted, FALSE) = FALSE
        WHERE m.id = ANY(:ids)
    """,
}


def get_titles(kind: str, ids: list[str]) -> pd.DataFrame:
    """Return id, title, parent_title, characters for the given ids of one content kind."""
    return run_user_query(_TITLE_SQL[kind], ids=[str(i) for i in ids])


# ---------------------------------------------------------------- user data

def get_user_characters(user_id: str) -> list[str]:
    """Names of the characters the user selected in their preferences."""
    df = run_user_query(
        """
        SELECT DISTINCT c.name
        FROM user_character_preference p
        JOIN "character" c ON c.id = p.character_id
        WHERE p.user_id = :user_id
          AND COALESCE(p.is_deleted, FALSE) = FALSE
          AND COALESCE(c.is_deleted, FALSE) = FALSE
        ORDER BY c.name
        """,
        user_id=user_id,
    )
    return df["name"].tolist()


def get_user_language(user_id: str) -> str | None:
    """The user's preferred language from user_metadata, or None when not set."""
    df = run_user_query("SELECT language FROM user_metadata WHERE id = :user_id", user_id=user_id)
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
        df = run_user_query(
            "SELECT DISTINCT user_id FROM ott_user_info WHERE lower(trim(email)) = lower(trim(:email))",
            email=email,
        )
    else:
        df = run_user_query(
            "SELECT DISTINCT user_id FROM ott_user_info WHERE trim(phone_number) = trim(:phone)",
            phone=phone_number,
        )
    return df["user_id"].dropna().astype(str).tolist()
