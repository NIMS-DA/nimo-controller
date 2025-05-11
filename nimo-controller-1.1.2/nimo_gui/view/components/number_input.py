"""Number Input"""
from typing import Literal, Optional
import streamlit as st
from streamlit.elements.widgets.number_input import Number
from streamlit.runtime.state.common import WidgetCallback, WidgetArgs, WidgetKwargs


def horizontal_label_number_input(
        label: str,
        min_value: Optional[Number] = None,
        max_value: Optional[Number] = None,
        value: Optional[Number] | Literal['min'] = 'min',
        step: Optional[Number] = None,
        format: Optional[str] = None,
        key: Optional[str] = None,
        help: Optional[str] = None,
        on_change: Optional[WidgetCallback] = None,
        args: Optional[WidgetArgs] = None,
        kwargs: Optional[WidgetKwargs] = None,
        placeholder: Optional[str] = None,
        disabled: bool = False,
        ) -> Optional[Number]:
    """Shows the number input control, with its label displayed to the left.

    Args:
        label (str): Label text.
        min_value (Optional[Number], optional): Minimum value able to be input. Defaults to None.
        max_value (Optional[Number], optional): Maximum value able to be input. Defaults to None.
        value (Optional[Number] | Literal['min'], optional): Initial value. Defaults to 'min'.
        step (Optional[Number], optional): The stepping interval. Defaults to 1 if the value is an int, 0.01 otherwise.
                                           If the value is not specified, the format parameter will be used.
        format (Optional[str], optional): Function used to format the input value. Defaults to None.
        help (Optional[str], optional): The help text. Defaults to None.
        key (Optional[Key], optional): The key used for the HTML component. Defaults to None.
        on_change (Optional[WidgetCallback], optional): The callback function called when the input value is changed. Defaults to None.
        args (Optional[WidgetArgs], optional): The positional arguments for the `on_change` callback function. Defaults to None.
        kwargs (Optional[WidgetKwargs], optional): The keyword arguments for the `on_change` callback function. Defaults to None.
        placeholder (Optional[str], optional): A string to display when the number input is empty. Defaults to "Choose an option".
        disabled (bool, optional): If True, the control will be inactive; otherwise, it will be active. Defaults to False.

    Returns:
        int: Input value.
    """
    with st.container():
        left, right = st.columns((3, 2), vertical_alignment='center')
        with left:
            st.markdown(label)
        with right:
            res = st.number_input(label, min_value=min_value, max_value=max_value, value=value, step=step,
                                  format=format, key=key, help=help, on_change=on_change, args=args, kwargs=kwargs,
                                  placeholder=placeholder, disabled=disabled, label_visibility='collapsed')
    return res

