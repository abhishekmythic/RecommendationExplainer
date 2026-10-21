import json
import os
import time

import pandas as pd
import redis
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

from db import (  # noqa: E402  (needs .env loaded first)
    MEMORY_TABLES,
    build_phone_number,
    country_options,
    find_user_ids,
    get_titles,
    get_user_characters,
    load_tables,
    pull_latest_dump,
)

from recs import (  # noqa: E402
    KINDS,
    LANGUAGES,
    SOURCES,
    add_matched_characters,
    fetch_top,
    get_client,
    lookup_user,
    refresh_sidebar,
    show_user_preferences,
    source_mix_badges,
    style_table,
)


st.title("Piecewise")
st.caption("See exactly what a user is recommended, and why: personalized, trending or discovery.")

try:
    client = get_client()
except redis.RedisError as e:
    st.error(f"Could not connect to Valkey: {e!r}")
    st.info(
        "Start your SSH tunnel to the Valkey cluster (forwarding localhost:6379), "
        "check REDIS_PASSWORD in .env, then reload this page."
    )
    st.stop()

refresh_sidebar()

try:
    tables = load_tables()
except Exception as e:
    st.error(f"Could not load the local dump: {e!r}")
    st.info(
        "Check that the local Postgres (main_character_dev) is running and that LOCAL_DB_URL is correct. "
        "If a table is missing, click Refresh Data in the sidebar to pull it."
    )
    st.stop()

with st.sidebar:
    st.divider()
    loaded = time.strftime("%H:%M:%S", time.localtime(tables.loaded_at))
    st.markdown(":green-badge[Valkey connected · read-only]")
    st.caption(f"{len(MEMORY_TABLES)} content tables loaded at {loaded}.")
    if tables.data_as_of is not None:
        st.caption(f"Newest record in the dump: {tables.data_as_of:%d %b %Y, %H:%M}.")

    st.divider()
    st.subheader("Source Legend")
    for label, color, _ in SOURCES.values():
        st.markdown(f":{color}-badge[{label}]")
    st.caption("Discover More items are random picks that fill out the list.")

selection = lookup_user("Show Recommendations")
if selection is not None:
    user_id, lang, top_n = selection

    preferred = show_user_preferences(user_id)

    st.subheader("Recommendations")
    for kind, kind_label in KINDS.items():
        try:
            df = fetch_top(client, user_id, lang, kind, top_n)
        except redis.RedisError as e:
            with st.expander(kind_label):
                st.error(f"Valkey error: {e}")
            continue
        with st.expander(f"{kind_label} · {len(df)} shown"):
            if df.empty:
                st.info(f"No {kind} recommendations were found for this user in {LANGUAGES[lang]}.")
            else:
                st.markdown(source_mix_badges(df))
                df = add_matched_characters(df, preferred)
                st.dataframe(
                    style_table(df),
                    hide_index=True,
                    width="stretch",
                    column_config={
                        "Rank": st.column_config.NumberColumn("Rank", width="small"),
                        "Title": st.column_config.TextColumn("Title", width="large"),
                    },
                )
