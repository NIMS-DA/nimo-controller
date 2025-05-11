"""Application entry point"""
from pathlib import Path
import streamlit as st
from nimo_gui.view.mainpage import main_page
from PIL import Image


APP_TITLE: str = 'NIMO'

HIDE_ST_STYLE = """
    <style>
        div[data-testid="stApp"] {
            background: #EEEEE;
        }
        div[data-testid="stApp"] div > section > div > div > div > div > div > div > div > div > div > div {
            background: #FFFFFF;
        }
        div[data-testid="stToolbar"] {
            visibility: hidden;
            height: 0%;
            position: fixed;
        }
        div[data-testid="stDecoration"] {
            visibility: hidden;
            height: 0%;
            position: fixed;
        }
        #MainMenu {
            visibility: hidden;
            height: 0%;
        }
        header {
            visibility: hidden;
            height: 0%;
        }
        footer {
            visibility: hidden;
            height: 0%;
        }
        .appview-container .main .block-container{
            padding-top: 1rem;
            padding-right: 3rem;
            padding-left: 3rem;
            padding-bottom: 1rem;
        }  
        .reportview-container {
            padding-top: 0rem;
            padding-right: 3rem;
            padding-left: 3rem;
            padding-bottom: 0rem;
        }
        header[data-testid="stHeader"] {
            z-index: -1;
        }
        div[data-testid="stToolbar"] {
            z-index: 100;
        }
        div[data-testid="stDecoration"] {
            z-index: 100;
        }
    </style>
"""

if __name__ == '__main__':
    icon_path: Path = Path(__file__).parent.joinpath('images/favicon.ico')
    st.set_page_config(
        page_title=APP_TITLE,
        page_icon=Image.open(icon_path),
        layout="wide",
    )

    st.write(HIDE_ST_STYLE, unsafe_allow_html=True)
    main_page()
