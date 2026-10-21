import redis
import streamlit as st
from dotenv import load_dotenv
from sqlalchemy.exc import SQLAlchemyError

load_dotenv()

from centralized import build_table  # noqa: E402
from recs import (  # noqa: E402
    KINDS,
    LANGUAGES,
    fetch_top,
    get_client,
    lookup_user,
    show_user_preferences,
    source_mix_badges,
    style_table,
)

st.title("Central")
st.caption(
    "All of a user's videos, audios and mantras in one list: batches of 10 with 6 videos, 3 audios and 1 mantra."
)

try:
    client = get_client()
except redis.RedisError as e:
    st.error(f"Could not connect to Valkey: {e!r}")
    st.info(
        "Start your SSH tunnel to the Valkey cluster (forwarding localhost:6379), "
        "check REDIS_PASSWORD in .env, then reload this page."
    )
    st.stop()

selection = lookup_user("Show Central")
if selection is None:
    st.stop()
user_id, lang, top_n = selection

show_user_preferences(user_id)

frames = {}
for kind, kind_label in KINDS.items():
    try:
        frames[kind] = fetch_top(client, user_id, lang, kind, top_n)
    except redis.RedisError as e:
        st.error(f"Valkey error while reading {kind_label}: {e}")
        st.stop()
    except SQLAlchemyError as e:
        st.error(f"Production database error while reading {kind_label}: {e!r}")
        st.stop()

table = build_table(frames, seed=f"{user_id}:{lang}:{top_n}")

st.subheader("Central Recommendations")
if table.empty:
    st.info(f"No recommendations were found for this user in {LANGUAGES[lang]}.")
    st.stop()

counts = {kind: len(frames[kind]) for kind in KINDS}
st.caption(
    f"{len(table)} items in {table['Batch'].max()} batches · "
    f"{counts['videos']} videos, {counts['audios']} audios, {counts['mantras']} mantras"
)
st.markdown(source_mix_badges(table))
st.dataframe(
    style_table(table.drop(columns=["Batch"])),
    hide_index=True,
    width="stretch",
    column_config={
        "Position": st.column_config.NumberColumn("Position", width="small"),
        "Rank": st.column_config.NumberColumn("Rank", width="small"),
        "Title": st.column_config.TextColumn("Title", width="large"),
    },
)
