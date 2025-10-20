def run():
    import streamlit.web.cli as stcli
    import sys
    sys.argv = ['streamlit','run','app/app.py','--server.headless','true']
    stcli.main()
