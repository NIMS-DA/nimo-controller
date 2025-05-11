"""Icon button"""
from typing import Optional
import streamlit as st
from streamlit.elements.lib.utils import Key
from streamlit.runtime.state.common import WidgetArgs, WidgetCallback, WidgetKwargs


def icon_button(
        label: str,
        icon_name: str,
        size: int,
        help: Optional[str]=None,
        key: Optional[Key]=None,
        on_click: Optional[WidgetCallback]=None,
        args: Optional[WidgetArgs]=None,
        kwargs: Optional[WidgetKwargs]=None,
        disabled: bool=False,
        use_container_width: bool=False,
        button_height: int=26) -> bool:
    st.markdown(
        f"""
        <style>
            .element-container:has(style){{
                display: none;
            }}
            #icon_button {{
                display: none;
            }}
            .element-container:has(#icon_button) {{
                display: none;
            }}
            .element-container:has(#icon_button) + div button {{
                border-width: 0;
                height: {button_height}px !important;
            }}
            .element-container:has(#icon_button) + div button p {{
                display: flex;
                flex-direction: column;
                align-items: center;
                font-family: "Source Sans Pro", sans-serif;
                font-size: 1.4rem;
                font-weight: 600;
                padding: 0.75rem 0px 1rem;
                margin: 0px;
                line-height: 1.2;
            }}
            .element-container:has(#icon_button) + div button p span {{
                font-size: {size}px !important;
                margin-top: -16px;
            }}
        </style>
        """,
        unsafe_allow_html=True,
    )
    st.markdown('<span id="icon_button"></span>', unsafe_allow_html=True)
    res = st.button(f'{label}  :material/{icon_name}:',
                    key=key,
                    help=help,
                    on_click=on_click,
                    args=args,
                    kwargs=kwargs,
                    disabled=disabled,
                    use_container_width=use_container_width)
    return res
