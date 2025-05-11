"""Select Box"""
from typing import Any, Callable, Optional, TypeVar
import streamlit as st
from streamlit.dataframe_util import OptionSequence
from streamlit.elements.lib.utils import Key
from streamlit.runtime.state.common import WidgetCallback, WidgetArgs, WidgetKwargs

T = TypeVar('T')

def horizontal_label_selectbox(
        label: str,
        options: OptionSequence[T],
        index: Optional[int] = 0,
        format_func: Callable[[Any], Any] = str,
        key: Optional[Key] = None,
        help: Optional[str] = None,
        on_change: Optional[WidgetCallback] = None,
        args: Optional[WidgetArgs] = None,
        kwargs: Optional[WidgetKwargs] = None,
        placeholder: str = 'Choose an option',
        disabled: bool = False,
        columns: Optional[tuple[int, int]] = None,
        ) -> Optional[T]:
    """Shows the selectbox control, with its label displayed to the left.

    Args:
        label (str): Label text.
        options (OptionSequence[T]): Options of the candidates list.
        index (Optional[int], optional): Index of the item selected initially. Defaults to 0.
        format_func (Callable[[Any], Any], optional): Function used to format the text of the items. Defaults to str.
        help (Optional[str], optional): The help text. Defaults to None.
        key (Optional[Key], optional): The key used for the HTML component. Defaults to None.
        on_change (Optional[WidgetCallback], optional): The callback function called when the selected value is changed. Defaults to None.
        args (Optional[WidgetArgs], optional): The positional arguments for the `on_change` callback function. Defaults to None.
        kwargs (Optional[WidgetKwargs], optional): The keyword arguments for the `on_change` callback function. Defaults to None.
        placeholder (str, optional): A string to display when no options are selected. Defaults to "Choose an option".
        disabled (bool, optional): If True, the control will be inactive; otherwise, it will be active. Defaults to False.
        columns (tuple[int, int], optional): 

    Returns:
        Optional[T]: _description_
    """
    if key is None:
        key = label
    if columns is None:
        columns = (3, 2)
    with st.container():
        left, right = st.columns(columns, vertical_alignment='center')
        with left:
            st.markdown(label)
        with right:
            res = st.selectbox(label, options, index=index, format_func=format_func, key=key,
                               help=help, on_change=on_change, args=args, kwargs=kwargs,
                               placeholder=placeholder, disabled=disabled, label_visibility='collapsed')
    return res