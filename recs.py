"""Helpers shared by every page: constants, the read-only Valkey client, table helpers and the user lookup form."""

import json
import os

import pandas as pd
import redis
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

from db import (  # noqa: E402  (needs .env loaded first)
    TABLE_NAMES,
    build_phone_number,
    country_options,
    find_user_ids,
    get_titles,
    get_user_characters,
    get_user_language,
    pull_latest_dump,
)

DEFAULT_TOP_N = 10
MAX_TOP_N = 200

# Conventions used in all text: Title Case for headings, buttons and column headers;
# sentence case for field labels and messages; US English spelling.
KINDS = {"videos": "Videos", "audios": "Audios", "mantras": "Mantras"}
LANGUAGES = {"HIN": "Hindi (HIN)", "ENG": "English (ENG)"}

# label for the parent-title column, per kind (mantras have no parent)
PARENT_LABEL = {"videos": "Series", "audios": "Audio Research"}

# source code -> (label, badge color, table cell color)
SOURCES = {
    "U": ("User Recommendation", "green", "rgba(46, 160, 67, 0.25)"),
    "T": ("Trending", "orange", "rgba(240, 136, 62, 0.25)"),
    "R": ("Discover More", "blue", "rgba(56, 139, 253, 0.25)"),
}

# content kind -> {type int -> label}
TYPES = {
    "videos": {0: "Episode", 3: "Season"},
    "audios": {6: "Audio Research", 7: "Chapter"},
    "mantras": {8: "Mantra"},
}

# Values of the Matched Characters column (main page only)
NO_CHARACTER_MATCH = "No character match"
NOT_PERSONALIZED = "Not personalized"
NO_PREFERENCES = "No preferences set"

# Production cache: this app must never write. Anything outside this set is refused client-side.
READ_ONLY_COMMANDS = {"PING", "LRANGE"}


class ReadOnlyRedis(redis.Redis):
    def execute_command(self, *args, **options):
        command = str(args[0]).upper()
        if command not in READ_ONLY_COMMANDS:
            raise PermissionError(f"Blocked non-read-only command: {command}")
        return super().execute_command(*args, **options)


@st.cache_resource(show_spinner="Connecting to Valkey...")
def get_client() -> ReadOnlyRedis:
    client = ReadOnlyRedis(
        host=os.getenv("REDIS_HOST", "localhost"),
        port=int(os.getenv("REDIS_PORT", "6379")),
        password=os.getenv("REDIS_PASSWORD") or None,
        ssl=os.getenv("REDIS_TLS", "true").lower() == "true",
        ssl_cert_reqs=None,  # via an SSH tunnel the cert hostname won't match
        decode_responses=True,
        socket_connect_timeout=5,
        socket_timeout=10,
    )
    client.ping()
    return client


def fetch_top(client: redis.Redis, user_id: str, lang: str, kind: str, top_n: int) -> pd.DataFrame:
    """Top recommendations for one user and content kind, with titles and characters."""
    key = f"recommendation:user:{user_id}:{lang}:{kind}"
    items = [json.loads(raw) for raw in client.lrange(key, 0, top_n - 1)]
    df = pd.DataFrame(items)
    if df.empty:
        return df
    titles = get_titles(kind, df["id"].tolist())
    df = df.merge(titles, on="id", how="left")
    df["title"] = df["title"].fillna("(Title not found)")
    df["type"] = df["type"].map(lambda t: TYPES[kind].get(t, f"Unknown ({t})"))
    df["source_code"] = df["source"]
    df["source"] = df["source"].map(lambda x: SOURCES.get(x, (f"Unknown ({x})",))[0])
    df.insert(0, "rank", range(1, len(df) + 1))
    df = df.rename(
        columns={"rank": "Rank", "type": "Type", "title": "Title", "characters": "Characters", "source": "Source"}
    )
    columns = ["Rank", "Type", "Title"]
    if kind in PARENT_LABEL:
        df = df.rename(columns={"parent_title": PARENT_LABEL[kind]})
        columns.append(PARENT_LABEL[kind])
    return df[columns + ["Characters", "Source", "source_code"]]


def _character_names(value) -> list[str]:
    """Names from a Characters cell: a ', '-joined string (get_titles) or a list; empty for None/NaN."""
    if value is None or (not isinstance(value, (list, tuple, set)) and pd.isna(value)):
        return []
    parts = value.split(",") if isinstance(value, str) else value
    return [str(p).strip() for p in parts if str(p).strip()]


def matched_characters(item_characters, preferred: list[str], source_code: str) -> str:
    """Matched Characters cell: item characters that are also user-preferred (case/whitespace-insensitive)."""
    if source_code != "U":
        return NOT_PERSONALIZED
    wanted = {" ".join(n.split()).casefold() for n in preferred if n and n.strip()}
    if not wanted:
        return NO_PREFERENCES
    matches = [n for n in _character_names(item_characters) if " ".join(n.split()).casefold() in wanted]
    unique = {}
    for n in matches:
        unique.setdefault(" ".join(n.split()).casefold(), n)  # first spelling wins
    return ", ".join(unique.values()) or NO_CHARACTER_MATCH


def add_matched_characters(df: pd.DataFrame, preferred: list[str]) -> pd.DataFrame:
    """Copy of a fetch_top frame with a Matched Characters column placed after Characters."""
    out = df.copy()
    matched = [matched_characters(c, preferred, s) for c, s in zip(out["Characters"], out["source_code"])]
    out.insert(out.columns.get_loc("Characters") + 1, "Matched Characters", matched)
    return out


def style_table(df: pd.DataFrame):
    """Color the Source column by bucket; the helper source_code column stays hidden."""
    colors = {label: color for label, _, color in SOURCES.values()}
    visible = df.drop(columns=["source_code"])
    return visible.style.map(
        lambda label: f"background-color: {colors[label]}" if label in colors else "", subset=["Source"]
    )


def source_mix_badges(df: pd.DataFrame) -> str:
    """e.g. ':green-badge[User Recommendation 4] :orange-badge[Trending 3]'"""
    counts = df["source_code"].value_counts()
    return " ".join(
        f":{color}-badge[{label} {counts[code]}]"
        for code, (label, color, _) in SOURCES.items()
        if code in counts
    )


def lookup_user(submit_label: str) -> tuple[str, str, int] | None:
    """Render the user lookup form; return (user_id, lang, top_n) once one user is found, else None.

    Shows the warning/error and stops the script run when the lookup fails.
    """
    # Either email or phone number, never both: the choice sits outside the form so the
    # inputs below switch as soon as it changes.
    search_by = st.radio("Find user by", ["Email", "Phone number"], horizontal=True)
    countries = country_options()

    with st.form("lookup"):
        if search_by == "Email":
            email = st.text_input("Email", placeholder="name@example.com")
        else:
            country_col, number_col = st.columns([1, 2])
            country = country_col.selectbox("Country", list(countries), index=0)
            number = number_col.text_input("Phone number (without country code)", placeholder="8910XXXXXX")
        language_col, count_col = st.columns(2)
        lang = language_col.radio("Language", list(LANGUAGES), format_func=LANGUAGES.get, horizontal=True)
        top_n = count_col.number_input(
            "Number of recommendations", min_value=1, max_value=MAX_TOP_N, value=DEFAULT_TOP_N, step=1
        )
        submitted = st.form_submit_button(submit_label, icon=":material/search:", type="primary")

    if not submitted:
        return None
    try:
        if search_by == "Email":
            if not email.strip():
                raise ValueError("Enter an email.")
            user_ids = find_user_ids(email=email.strip())
        else:
            user_ids = find_user_ids(phone_number=build_phone_number(countries[country], number))
    except ValueError as e:
        st.warning(str(e))
        st.stop()
    except Exception as e:
        st.error(f"Could not look the user up in production: {e!r}")
        st.info("User data is read live from production over SSH. Check your network and the SSH_* and DB_* values in .env.")
        st.stop()

    if not user_ids:
        st.warning(f"No user was found for that {search_by.lower()}.")
        st.stop()
    if len(user_ids) > 1:
        st.error("More than one user matches: " + ", ".join(user_ids))
        st.stop()
    return user_ids[0], lang, top_n


def refresh_sidebar() -> None:
    """Sidebar section to pull chosen tables from production into the local dump."""
    with st.sidebar:
        st.header("Data")
        st.caption(
            "Pulls the chosen tables from production into your local dump. "
            "Production is only read, never changed."
        )
        def set_all() -> None:
            for t in TABLE_NAMES:
                st.session_state[f"refresh_table_{t}"] = st.session_state["refresh_all_tables"]

        # keep "Select all" in step with the individual boxes
        st.session_state["refresh_all_tables"] = all(
            st.session_state.get(f"refresh_table_{t}", False) for t in TABLE_NAMES
        )
        with st.popover("Select Tables", icon=":material/arrow_drop_down:", width="stretch"):
            st.checkbox("Select all", key="refresh_all_tables", on_change=set_all)
            st.divider()
            chosen = [t for t in TABLE_NAMES if st.checkbox(t, key=f"refresh_table_{t}")]
        st.caption(f"{len(chosen)} of {len(TABLE_NAMES)} tables selected." if chosen else "No tables selected.")
        if st.button("Refresh Data", icon=":material/sync:", width="stretch", disabled=not chosen):
            with st.status(f"Pulling {len(chosen)} table(s) from production...", expanded=True) as status:
                bar = st.progress(0.0, text="Starting...")
                try:
                    counts = pull_latest_dump(on_progress=lambda msg, frac: bar.progress(frac, text=msg), tables=chosen)
                except Exception as e:
                    status.update(label="Refresh failed. The local dump was not changed.", state="error")
                    st.error(repr(e))
                else:
                    status.update(label=f"Pulled {len(counts)} table(s), {sum(counts.values()):,} rows.", state="complete")


def show_user_preferences(user_id: str) -> list[str]:
    """Render the User Preferences box (preferred language and characters); return the character names."""
    language = get_user_language(user_id)
    preferred = get_user_characters(user_id)
    with st.container(border=True):
        st.subheader("User Preferences")
        st.markdown(f"**User ID:** `{user_id}`")
        if language:
            st.markdown(f"**Preferred language:** :blue-badge[{LANGUAGES.get(language.upper(), language)}]")
        else:
            st.markdown("**Preferred language:** none set")
        if preferred:
            st.markdown("**Preferred characters:** " + " ".join(f":violet-badge[{name}]" for name in preferred))
        else:
            st.markdown("**Preferred characters:** none selected")
    return preferred
