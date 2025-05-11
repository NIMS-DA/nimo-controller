"""Message dialogs"""
import streamlit as st

from nimo_gui.state import AppState


@st.dialog('Completed', width='small')
def completed_dialog():
    """Shows a complete message in the dialog."""
    st.write('The NIMO process has been completed successfully.')
    _, button_area = st.columns((4, 1))
    with button_area:
        if st.button('OK', use_container_width=True):
            st.rerun()


@st.dialog('Error', width='small')
def worker_error_dialog(message: str):
    """Shows an error message in the dialog.

    Args:
        message (str): The error message.
    """
    st.write('The NIMO process has been interrupted by the following error.')
    st.code(message, language=None)
    _, button_area = st.columns((4, 1))
    with button_area:
        if st.button('OK', use_container_width=True):
            st.rerun()


@st.dialog('Error', width='small')
def parameter_error_dialog(message: str):
    """Shows an error message in the dialog.

    Args:
        message (str): The error message.
    """
    st.write(message)
    _, button_area = st.columns((4, 1))
    with button_area:
        if st.button('OK', use_container_width=True):
            st.rerun()


@st.dialog('Confirmation', width='small')
def reset_confirmation_dialog(state: AppState):
    st.write('If you reset the parameters, the process log and plot data will also be deleted.')
    st.write('Do you want to continue?')
    _, ok_button_area, cancel_button_area = st.columns((3, 1, 1))
    with ok_button_area:
        if st.button('OK', use_container_width=True):
            state.reset_parameters()
            st.rerun()
    with cancel_button_area:
        if st.button('Cancel', use_container_width=True):
            st.rerun()
