"""Text Input"""
from typing import Literal, Optional
import streamlit as st
from streamlit.runtime.state.common import WidgetCallback, WidgetArgs, WidgetKwargs


def horizontal_label_text_input(
        label: str,
        value: Optional[str] = None,
        max_chars: Optional[int] = None,
        key: str | None = None,
        type: Literal['default', 'password'] = 'default',
        help: str | None = None,
        on_change: WidgetCallback | None = None,
        args: WidgetArgs | None = None,
        kwargs: WidgetKwargs | None = None,
        placeholder: str | None = None,
        disabled: bool = False,
        ) -> Optional[str]:
    """Shows the text input control, with its label displayed to the left.

    Args:
        label (str): Label text.
        value (Number | Literal['min'], optional): Initial value. Defaults to 'min'.
        max_chars (Optional[int], optional): Max number of characters allowed in text input.
        key (Optional[Key], optional): The key used for the HTML component. Defaults to None.
        type (Literal['default', 'password']): The type of the text input. Defaults to `default`.
        help (Optional[str], optional): The help text. Defaults to None.
        on_change (WidgetCallback | None, optional): The callback function called when the input value is changed. Defaults to None.
        args (Optional[WidgetArgs], optional): The positional arguments for the `on_change` callback function. Defaults to None.
        kwargs (Optional[WidgetKwargs], optional): The keyword arguments for the `on_change` callback function. Defaults to None.
        placeholder (str | None, optional): A string to display when the number input is empty. Defaults to "Choose an option".
        disabled (bool, optional): If True, the control will be inactive; otherwise, it will be active. Defaults to False.

    Returns:
        int: Input value.
    """
    with st.container():
        left, right = st.columns((3, 2), vertical_alignment='center')
        with left:
            st.markdown(label)
        with right:
            res = st.text_input(label, value=value, max_chars=max_chars, key=key, type=type, help=help, on_change=on_change,
                                args=args, kwargs=kwargs, placeholder=placeholder, disabled=disabled, label_visibility='collapsed')
    return res

