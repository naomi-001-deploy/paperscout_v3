import streamlit as st, sys, platform
st.set_page_config(page_title="hello", layout="wide")
st.title("✅ Streamlit Cloud läuft")
st.write("Python:", sys.version)
st.write("Platform:", platform.platform())
