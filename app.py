import streamlit as st

st.set_page_config(page_title="Recommendation Explainer", page_icon=":material/insights:", layout="wide")

pages = [
    st.Page("pages/Central.py", title="Central", icon=":material/view_list:", default=True),
    st.Page("pages/Piecewise.py", title="Piecewise", icon=":material/insights:"),
]
st.navigation(pages).run()
