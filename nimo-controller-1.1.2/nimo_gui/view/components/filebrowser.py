"""File browser"""
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import streamlit as st
from streamlit.delta_generator import DeltaGenerator
from streamlit.elements.lib.utils import Key
from streamlit.runtime.state.common import WidgetArgs, WidgetCallback, WidgetKwargs


@dataclass
class _LocalState:
    current_path: Optional[Path] = None 
    selected_file: Optional[Path] = None
    last_selected_file: Optional[Path] = None


@st.dialog('Select your input CSV.', width='large')
def show_input_file_browser(
        initial_path: Path=Path('/'),
        on_select: Optional[Callable[[Path], None]]=None
        ) -> None:
    """Shows the file browser dialog used to select the input file.

    Args:
        initial_path (Path, optional): The path of the folder shown initially. Defaults to Path('/').
        on_select (Optional[Callable[[Path], None]], optional): The callback function called when the input file is selected. Defaults to None.
    """
    show_file_browser(initial_path, on_select, filter=['.csv'])


@st.dialog('Select your custom AI algorithm.', width='large')
def show_ai_script_file_browser(
        initial_path: Path=Path('/'),
        on_select: Optional[Callable[[Path], None]]=None
        ) -> None:
    """Shows the file browser dialog used to select the customised AI-algorithm file.

    Args:
        initial_path (Path, optional): The path of the folder shown initially. Defaults to Path('/').
        on_select (Optional[Callable[[Path], None]], optional): The callback function called when the AI-algorithm file is selected. Defaults to None.
    """
    show_file_browser(initial_path, on_select)


@st.dialog('Select your custom robotic system.', width='large')
def show_robot_script_file_browser(
        initial_path: Path=Path('/'),
        on_select: Optional[Callable[[Path], None]]=None
        ) -> None:
    """Shows the file browser dialog used to select the customised robot-operation file.

    Args:
        initial_path (Path, optional): The path of the folder shown initially. Defaults to Path('/').
        on_select (Optional[Callable[[Path], None]], optional): The callback function called when the robot script file is selected. Defaults to None.
    """
    show_file_browser(initial_path, on_select)


@st.dialog('Select your custom analysis script.', width='large')
def show_analysis_file_browser(
        initial_path: Path=Path('/'),
        on_select: Optional[Callable[[Path], None]]=None
        ) -> None:
    """Shows the file browser dialog used to select the customised analysis file.

    Args:
        initial_path (Path, optional): The path of the folder shown initially. Defaults to Path('/').
        on_select (Optional[Callable[[Path], None]], optional): The callback function called when the analysis file is selected. Defaults to None.
    """
    show_file_browser(initial_path, on_select)


def show_file_browser(
        initial_path: Path=Path('/'),
        on_select: Optional[Callable[[Path], None]] = None,
        filter: Optional[list[str]] = None,
        show_hidden_directory: bool = False,
        show_hidden_files: bool = False,
        ) -> None:
    state: _LocalState = get_local_state()
    if state.current_path is None:
        state.current_path = initial_path
    
    if filter is not None:
        filter = [ext.lower() for ext in filter]

    # Apply styles.
    apply_style(icon_size=22)

    container: DeltaGenerator = st.container(height=250, border=True)

    assert state.current_path is not None
    file_list(container, state.current_path, filter, show_hidden_directory, show_hidden_files)

    textbox_area, button_area = st.columns((10, 1))
    with textbox_area:
        key: str = 'selected_file_text_box'
        file_path: str = str(state.selected_file) if state.selected_file is not None else ''
        st.text_input(label=key, value=file_path, key=key, label_visibility='collapsed')
    with button_area:
        button_disabled: bool = state.selected_file is None
        if st.button('OK', disabled=button_disabled, use_container_width=True):
            if state.selected_file is not None:
                state.last_selected_file = state.selected_file
                state.selected_file = None
                if on_select is not None:
                    assert state.last_selected_file is not None
                    on_select(state.last_selected_file)
                st.rerun()


def get_local_state() -> _LocalState:
    state = st.session_state
    if 'local' not in state:
        state['local'] = {}
    if 'file_browser' not in state['local']:
        state['local']['file_browser'] = _LocalState()
    return state['local']['file_browser']


def file_list(
        container: DeltaGenerator,
        path: Path,
        filter: Optional[list[str]],
        show_hidden_directory: bool,
        show_hidden_files: bool):

    folders: list[Path] = []
    files: list[Path] = []
    def name(path: Path) -> str:
        return path.name
    
    # Split into subdirectories and files.
    for item in path.iterdir():
        if item.is_dir():
            if not show_hidden_directory and item.name.startswith('.'):
                continue
            folders.append(item)
        elif item.is_file():
            if filter is not None and item.suffix.lower() not in filter:
                continue
            if not show_hidden_files and item.name.startswith('.'):
                continue
            files.append(item)
    
    # Sort by name.
    folders.sort(key=name)
    files.sort(key=name)

    # Combine the lists.
    items: list[Path] = folders + files
    total_count: int = len(items)

    # Place buttons for each element.
    upto_parent_required: bool = str(path) != '/'
    i: int = 0 if not upto_parent_required else -1
    while i < total_count:
        cols = container.columns((1, 1, 1))
        for col in cols:
            if upto_parent_required:
                upto_parent_required = False
                parent_folder_button(col, path.parent)
            else:
                item: Path = items[i]
                if item.is_dir():
                    folder_button(col, item)
                elif item.is_file():
                    file_button(col, item)                   
            i += 1
            if i == total_count:
                break


def parent_folder_button(container: DeltaGenerator, parent: Path) -> bool:
    return item_button(
        container,
        f':material/drive_folder_upload: &nbsp; ..',
        on_click=on_folder_click,
        kwargs={'folder': parent},
        key='upto_parent')


def folder_button(container: DeltaGenerator, folder: Path) -> bool:
    """Shows the folder button.

    Args:
        container (DeltaGenerator): _description_
        folder (Path): _description_

    Returns:
        bool: _description_
    """
    return item_button(
        container,
        f':material/folder: &nbsp; {folder.name}',
        on_click=on_folder_click,
        kwargs={'folder': folder},
        key=folder.name)


def file_button(container: DeltaGenerator, file: Path) -> bool:
    return item_button(
        container,
        f':material/description: &nbsp; {file.name}',
        on_click=on_file_click,
        kwargs={'file': file},
        key=file.name)


def on_folder_click(folder: Path) -> None:
    """The callback function called when the folder buttons are clicked.

    Args:
        folder (Path): Path of the folder clicked.
    """
    state: _LocalState = get_local_state()
    state.current_path = folder


def on_file_click(file: Path) -> None:
    """The callback function called when the file buttons are clicked.

    Args:
        file (Path): Path of the file clicked.
    """
    state: _LocalState = get_local_state()
    state.selected_file = file


def item_button(
        container: DeltaGenerator,
        label: str,
        help: Optional[str]=None,
        key: Optional[Key]=None,
        on_click: Optional[WidgetCallback]=None,
        args: Optional[WidgetArgs] = None,
        kwargs: Optional[WidgetKwargs] = None) -> bool:
    """The button component used to represent folders and files within the file browser component.

    Args:
        container (DeltaGenerator): The container component in which the button is placed.
        label (str): The label text.
        help (Optional[str], optional): The help text. Defaults to None.
        key (Optional[Key], optional): The key used for the HTML component. Defaults to None.
        on_click (Optional[WidgetCallback], optional): The function called when the button is clicked. Defaults to None.
        args (Optional[WidgetArgs], optional): The positional arguments for the `on_click` callback function. Defaults to None.
        kwargs (Optional[WidgetKwargs], optional): The keyword arguments for the `on_click` callback function. Defaults to None.

    Returns:
        bool: Returns True when the button is clicked; otherwise, False.
    """
    container.markdown('<span id="file_browser_item"></span>', unsafe_allow_html=True)
    return container.button(label, key=key, help=help, on_click=on_click, args=args, kwargs=kwargs)


def apply_style(icon_size: int=20, item_height: int=30) -> None:
    """Defines the style used to customize the visuals of the icon buttons.

    Args:
        icon_size (int): Icon size (pixel).
        item_height (int): Button height (pixel).
    """
    st.markdown(
        f"""
        <style>
            .element-container:has(style){{
                display: none;
            }}
            #file_browser_item {{
                display: none;
            }}
            .element-container:has(#file_browser_item) {{
                display: none;
            }}
            .element-container:has(#file_browser_item) + div button {{
                height: {item_height}px !important;
                border-width: 0;
            }}
            .element-container:has(#file_browser_item) + div button p {{
                display: flex;
                align-items: center;
            }}
            .element-container:has(#file_browser_item) + div button p span {{
                font-size: {icon_size}px !important;
            }}
        </style>
        """,
        unsafe_allow_html=True,
    )
