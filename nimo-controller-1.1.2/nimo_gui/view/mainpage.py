"""Main page of the application"""
from datetime import timedelta
from io import BytesIO
import math
import os
from pathlib import Path
import signal
import time
from typing import Literal, Optional

from PIL import Image
import streamlit as st
from streamlit.delta_generator import DeltaGenerator
from streamlit.elements.widgets.button import DownloadButtonDataType

from nimo_gui.algorithm_options import BloxOptions, PdcOptions, PhysboOptions, PtrOptions, ReOptions, SlesaOptions
from nimo_gui.constants import (AI_ALGORITHM_BLOX, AI_ALGORITHM_ORIGINAL, AI_ALGORITHM_PDC, AI_ALGORITHM_PHYSBO,
                                AI_ALGORITHM_PTR, AI_ALGORITHM_RE, AI_ALGORITHM_SLESA, AI_ALGORITHMS, APP_STATE_KEY,
                                DEFAULT_AI_ALGORITHM, DEFAULT_PROPOSALS, DEFAULT_ROBOT_SYSTEM, MAX_CYCLES,
                                MAX_OBJECTIVES, MAX_PROPOSALS, OPTIONS_PDC_ESTIMATION, OPTIONS_PDC_SAMPLING,
                                OPTIONS_PHYSBO_SCORE_MULTI, OPTIONS_PHYSBO_SCORE_SINGLE, ROBOT_SYSTEMS,
                                WORKING_STATE_CONFIGURATION, WORKING_STATE_SUSPENDED, WORKING_STATE_RUN, WORKING_STATE_SUSPENDING)
from nimo_gui.state import AppState
from nimo_gui.view.components.button import icon_button
from nimo_gui.view.components.filebrowser import (show_ai_script_file_browser, show_analysis_file_browser,
                                                  show_input_file_browser, show_robot_script_file_browser)
from nimo_gui.view.components.number_input import horizontal_label_number_input
from nimo_gui.view.components.selectbox import horizontal_label_selectbox
from nimo_gui.view.dialogs import completed_dialog, parameter_error_dialog, reset_confirmation_dialog, worker_error_dialog


def main_page() -> None:
    """Show the main page of the application."""
    state: AppState = get_app_state()
    set_event_listener(state)
    placeholders: dict[str, DeltaGenerator | list[DeltaGenerator]] = {}

    logo = Image.open(Path(__file__).parent.parent.joinpath('images/NIMO.png'))
    st.image(logo, width=100)

    left, center, right = st.columns((1, 1, 1))

    with left:
        parameters_panel(state)
        state.csv_file = Path(input_file_panel(state))
        cycle_program_list_panel(state)

    with center:
        placeholders['progress'] = control_panel(state)
        placeholders['cycle_counter'] = cycle_counter_panel()
        placeholders.update(time_panel())

    with right:
        placeholders['output'] = output_panel(state)
        tab_labels: list[str] = (['Phase', 'Phase diagram'] if state.is_pdc_used()
                                 else ['Distribution']) + ['Objectives', 'Best objective']
        placeholders.update(results_panel(tab_labels, state))
    
    if state.working_state in ['run', 'suspending']:
        do_observer_loop(placeholders, state)
    else:
        update_placeholders(placeholders, state)
        update_analysis(placeholders, state)


def parameters_panel(state: AppState) -> None:
    """Shows the parameter setting panel.

    Args:
        state (AppState): The application state.
    """
    with st.container(border=True):
        is_disabled: bool = state.working_state != 'configuration'
        st.markdown(f'#### Parameters')

        key_num_objects: str = 'selectbox-num_objectives'
        horizontal_label_selectbox(
            'Number of objectives',
            range(1, MAX_OBJECTIVES + 1),
            index=state.num_objectives - 1,
            key=key_num_objects,
            on_change=on_control_change,
            kwargs={'property_key': 'num_objectives', 'control_key': key_num_objects},
            disabled=is_disabled)
        
        key_num_cycles: str = 'number_input-num_cycles'
        horizontal_label_number_input(
            'Number of cycles',
            min_value=1,
            max_value=MAX_CYCLES,
            value=state.num_cycles,
            key=key_num_cycles,
            on_change=on_control_change,
            kwargs={'property_key': 'num_cycles', 'control_key': key_num_cycles},
            disabled=is_disabled)


def input_file_panel(state: AppState):
    """Shows the input file setting panel.

    Args:
        state (AppState): The application state.
    """
    with st.container(border=True):
        st.markdown(f'#### Candidates file (CSV)')
        left, right = st.columns((9, 1))
        is_disabled: bool = state.working_state != 'configuration'

        with left:
            file_path: str = '' if state.csv_file is None else str(state.csv_file)
            value = st.text_input('input_file_text_box', value=file_path, disabled=is_disabled, label_visibility='collapsed')
        with right:
            st.button(
                ':material/folder_open:',
                type='secondary',
                use_container_width=True,
                help='Browse folders',
                on_click=show_input_file_browser,
                kwargs={'on_select': on_select_input_file})
        return value


def cycle_program_list_panel(state:AppState):
    """Shows the cycle program list panel.

    Args:
        state (AppState): The application state.
    """
    is_disabled: bool = state.working_state != 'configuration'
    with st.container(border=True):
        title, button1, button2, button3 = st.columns((7, 1, 1, 1))
        with title:
            st.markdown(f'#### Cycle program')
        with button1:
            st.button(
                ':material/smart_toy:',
                type='secondary',
                use_container_width=True,
                help='Select the custom AI script.',
                on_click=show_ai_script_file_browser,
                kwargs={'on_select': on_select_ai_script},
                disabled=is_disabled)
        with button2:
            st.button(
                ':material/precision_manufacturing:',
                type='secondary',
                use_container_width=True,
                help='Select the custom robot-operation script.',
                on_click=show_robot_script_file_browser,
                kwargs={'on_select': on_select_robot_script},
                disabled=is_disabled)
        with button3:
            st.button(
                ':material/finance:',
                type='secondary',
                use_container_width=True,
                help='Select the custom analysis script.',
                on_click=show_analysis_file_browser,
                kwargs={'on_select': on_select_analysis_script},
                disabled=is_disabled)
        
        with st.container(height=400, border=False):
            cycle_program_header()
            if len(state.ai_algorithms) < state.num_cycles:
                state.ai_algorithms += [DEFAULT_AI_ALGORITHM for _ in range(state.num_cycles - len(state.ai_algorithms))]
            if len(state.num_proposals) < state.num_cycles:
                state.num_proposals += [DEFAULT_PROPOSALS for _ in range(state.num_cycles - len(state.num_proposals))]
            if len(state.robotic_systems) < state.num_cycles:
                state.robotic_systems += [DEFAULT_ROBOT_SYSTEM for _ in range(state.num_cycles - len(state.robotic_systems))]
            for cycle_id in range(state.num_cycles):
                cycle_program_panel(cycle_id, is_disabled, state.ai_algorithms[cycle_id], state.num_proposals[cycle_id], state.robotic_systems[cycle_id])
        
        ai_algorithm_option_panel()
        

def cycle_program_header():
    """Shows the header line of the cycle program list."""
    _, ai, proposal, robot = st.columns((1, 6, 4, 6), vertical_alignment='center')
    with ai:
        st.markdown('<center>AI algorithm</center>', unsafe_allow_html=True)
    with proposal:
        st.markdown('<center>Proposals</center>', unsafe_allow_html=True)
    with robot:
        st.markdown('<center>Robotic system</center>', unsafe_allow_html=True)


def cycle_program_panel(
        index: int,
        fixed: bool,
        ai_algorithm: str,
        num_proposals: int,
        robot_system: str
        ) -> None:
    """Shows the cycle program setting panel.

    Args:
        index (int): Index of the target step.
        editable (bool): Give True if parameters are editable; Otherwise False.
        ai_algorithm (str): The AI algorithm.
        num_proposals (int): The number of proposals.
        robot_system (str): The robotic system.
    """
    with st.container():
        cycle, ai, proposal, robot = st.columns((1, 6, 4, 6), vertical_alignment='center')

        with cycle:
            st.markdown(f'{index + 1}:')

        with ai:
            key_ai_algorithm: str = f'selectbox-algorithm-{index}'
            st.selectbox(
                'Algorithm',
                AI_ALGORITHMS,
                index=AI_ALGORITHMS.index(ai_algorithm),
                disabled=fixed,
                key=key_ai_algorithm,
                help='Select the AI algorithm.',
                on_change=on_program_option_change,
                kwargs={'property_key': 'ai_algorithms', 'index': index, 'control_key': key_ai_algorithm},
                label_visibility='collapsed')
            
        with proposal:
            key_num_proposals: str = f'selectbox-num-proposals-{index}'
            st.selectbox(
                'Number of proposals',
                range(1, MAX_PROPOSALS + 1),
                index=num_proposals - 1,
                disabled=fixed,
                key=key_num_proposals,
                help='Select the number of the proposals.',
                on_change=on_program_option_change,
                kwargs={'property_key': 'num_proposals', 'index': index, 'control_key': key_num_proposals},
                label_visibility='collapsed')
            
        with robot:
            key_robotic_system: str = f'selectbox-robotic_system-{index}'
            st.selectbox(
                'Robotic system',
                ROBOT_SYSTEMS,
                index=ROBOT_SYSTEMS.index(robot_system),
                disabled=fixed,
                key=key_robotic_system,
                help='Select the robotic system.',
                on_change=on_program_option_change,
                kwargs={'property_key': 'robotic_systems', 'index': index, 'control_key': key_robotic_system},
                label_visibility='collapsed')


def ai_algorithm_option_panel():
    """Shows the setting panel of AI algorithm options."""
    state: AppState = get_app_state()
    is_disabled: bool = state.working_state != 'configuration'
    with st.expander('Options'):
        algorithms = state.selected_ai_ai_algorithms
        if AI_ALGORITHM_ORIGINAL in algorithms:
            algorithms.remove(AI_ALGORITHM_ORIGINAL)
        if 0 < len(algorithms):
            if state.option_ai_algorithm not in algorithms:
                state.option_ai_algorithm = algorithms[0]
            key_options_ai_algorithm: str = 'options-ai_algorithm'
            horizontal_label_selectbox(
                'AI algorithm',
                algorithms,
                index=algorithms.index(state.option_ai_algorithm),
                key=key_options_ai_algorithm,
                on_change=on_control_change,
                kwargs={'property_key': 'option_ai_algorithm', 'control_key': key_options_ai_algorithm},
                disabled=is_disabled,
                columns=(1, 2))
            if state.option_ai_algorithm == AI_ALGORITHM_RE:
                re_options()
            elif state.option_ai_algorithm == AI_ALGORITHM_PHYSBO:
                physbo_options()
            elif state.option_ai_algorithm == AI_ALGORITHM_BLOX:
                blox_options()
            elif state.option_ai_algorithm == AI_ALGORITHM_PDC:
                pdc_options()
            elif state.option_ai_algorithm == AI_ALGORITHM_PTR:
                ptr_options()
            elif state.option_ai_algorithm == AI_ALGORITHM_SLESA:
                slesa_options()

def re_options():
    """Shows the input forms for options of RE algorithm."""
    state: AppState = get_app_state()
    is_disabled: bool = state.working_state != 'configuration'
    option_parameters: ReOptions = state.ai_algorithm_options.re

    key_re_seed: str = 'number_input-re_re_seed'
    horizontal_label_number_input(
        '* re_seed',
        min_value=1,
        value=option_parameters.re_seed,
        key=key_re_seed,
        on_change=on_control_change,
        kwargs={'property_key': 're_seed', 'control_key': key_re_seed, 'target': option_parameters},
        disabled=is_disabled
    )


def physbo_options():
    """Shows the input forms for options of PHYSBO algorithm."""
    state: AppState = get_app_state()
    is_disabled: bool = state.working_state != 'configuration'
    option_parameters: PhysboOptions = state.ai_algorithm_options.physbo_single if \
        state.num_objectives == 1 else state.ai_algorithm_options.physbo_multi
    score_items: list[str] = OPTIONS_PHYSBO_SCORE_SINGLE if state.num_objectives == 1 else OPTIONS_PHYSBO_SCORE_MULTI

    key_score: str = 'select_box-physbo_score'
    horizontal_label_selectbox(
        label='* physbo_score',
        options=score_items,
        index=score_items.index(option_parameters.physbo_score),
        key=key_score,
        on_change=on_control_change,
        kwargs={'property_key': 'physbo_score', 'control_key': key_score, 'target': option_parameters},
        disabled=is_disabled
    )

    key_ard: str = 'select_box-physbo_ard'
    horizontal_label_selectbox(
        label='* ard',
        options=['True', 'False'],
        index=0 if option_parameters.ard else 1,
        key=key_ard,
        on_change=on_control_change,
        kwargs={'property_key': 'ard', 'control_key': key_ard, 'target': option_parameters},
        disabled=is_disabled
    )

    key_output_res: str = 'select_box-physbo_output_res'
    horizontal_label_selectbox(
        label='* output_res',
        options=['True', 'False'],
        index=0 if option_parameters.output_res else 1,
        key=key_output_res,
        on_change=on_control_change,
        kwargs={'property_key': 'output_res', 'control_key': key_output_res, 'target': option_parameters},
        disabled=is_disabled
    )


def blox_options():
    """Shows the input forms for options of BLOX algorithm."""
    state: AppState = get_app_state()
    is_disabled: bool = state.working_state != 'configuration'
    option_parameters: BloxOptions = state.ai_algorithm_options.blox

    key_output_res: str = 'select_box-blox_output_res'
    horizontal_label_selectbox(
        label='* output_res',
        options=['True', 'False'],
        index=0 if option_parameters.output_res else 1,
        key=key_output_res,
        on_change=on_control_change,
        kwargs={'property_key': 'output_res', 'control_key': key_output_res, 'target': option_parameters},
        disabled=is_disabled
    )


def pdc_options():
    """Shows the input forms for options of PDC algorithm."""
    state: AppState = get_app_state()
    is_disabled: bool = state.working_state != 'configuration'
    option_parameters: PdcOptions = state.ai_algorithm_options.pdc

    key_estimation: str = 'select_box-pdc_estimation'
    horizontal_label_selectbox(
        label='* pdc_estimation',
        options=OPTIONS_PDC_ESTIMATION,
        index=OPTIONS_PDC_ESTIMATION.index(option_parameters.pdc_estimation),
        key=key_estimation,
        on_change=on_control_change,
        kwargs={'property_key': 'pdc_estimation', 'control_key': key_estimation, 'target': option_parameters},
        disabled=is_disabled
    )

    key_sampling: str = 'select_box-pdc_sampling'
    horizontal_label_selectbox(
        label='* pdc_sampling',
        options=OPTIONS_PDC_SAMPLING,
        index=OPTIONS_PDC_SAMPLING.index(option_parameters.pdc_sampling),
        key=key_sampling,
        on_change=on_control_change,
        kwargs={'property_key': 'pdc_sampling', 'control_key': key_sampling, 'target': option_parameters},
        disabled=is_disabled
    )

    key_output_res: str = 'select_box-pdc_output_res'
    horizontal_label_selectbox(
        label='* output_res',
        options=['True', 'False'],
        index=0 if option_parameters.output_res else 1,
        key=key_output_res,
        on_change=on_control_change,
        kwargs={'property_key': 'output_res', 'control_key': key_output_res, 'target': option_parameters},
        disabled=is_disabled
    )


def slesa_options():
    """Shows the input forms for options of SLESA algorithm."""
    state: AppState = get_app_state()
    is_disabled: bool = state.working_state != 'configuration'
    option_parameters: SlesaOptions = state.ai_algorithm_options.slesa

    key_slesa_beta_max: str = 'number_input-slesa_beta_max'
    horizontal_label_number_input(
        '* slesa_beta_max',
        value=option_parameters.slesa_beta_max,
        key=key_slesa_beta_max,
        on_change=on_control_change,
        kwargs={'property_key': 'slesa_beta_max', 'control_key': key_slesa_beta_max, 'target': option_parameters},
        disabled=is_disabled
    )

    key_slesa_beta_num: str = 'number_input-slesa_beta_num'
    horizontal_label_number_input(
        '* slesa_beta_num',
        value=option_parameters.slesa_beta_num,
        key=key_slesa_beta_num,
        on_change=on_control_change,
        kwargs={'property_key': 'slesa_beta_num', 'control_key': key_slesa_beta_num, 'target': option_parameters},
        disabled=is_disabled
    )

    key_re_seed: str = 'number_input-slesa_re_seed'
    horizontal_label_number_input(
        '* re_seed',
        value=option_parameters.re_seed,
        key=key_re_seed,
        on_change=on_control_change,
        kwargs={'property_key': 're_seed', 'control_key': key_re_seed, 'target': option_parameters},
        disabled=is_disabled
    )

    key_output_res: str = 'select_box-slesa_output_res'
    horizontal_label_selectbox(
        label='* output_res',
        options=['True', 'False'],
        index=0 if option_parameters.output_res else 1,
        key=key_output_res,
        on_change=on_control_change,
        kwargs={'property_key': 'output_res', 'control_key': key_output_res, 'target': option_parameters},
        disabled=is_disabled
    )


def ptr_options():
    """Shows the input forms for options of PTR algorithm."""
    state: AppState = get_app_state()
    is_disabled: bool = state.working_state != 'configuration'
    option_parameters: PtrOptions = state.ai_algorithm_options.ptr

    key_output_res: str = 'select_box-ptr_output_res'
    horizontal_label_selectbox(
        label='* output_res',
        options=['True', 'False'],
        index=0 if option_parameters.output_res else 1,
        key=key_output_res,
        on_change=on_control_change,
        kwargs={'property_key': 'output_res', 'control_key': key_output_res, 'target': option_parameters},
        disabled=is_disabled
    )

    st.markdown('* ptr_ranges')
    ptr_ranges = state.ai_algorithm_options.ptr.ptr_ranges
    ptr_ranges_header()
    for i in range(state.num_objectives):
        if i == len(ptr_ranges):
            ptr_ranges.append(['min', 'max'])
        ptr_ranges_panel(i, ptr_ranges[i], is_disabled)


def ptr_ranges_header():
    """Shows the header row of the 'ptr_ranges' option list."""
    _, min, max = st.columns((2, 5, 5), vertical_alignment='center')
    with min:
        st.markdown('<center>min</center>', unsafe_allow_html=True)
    with max:
        st.markdown('<center>max</center>', unsafe_allow_html=True)


def ptr_ranges_panel(
        index: int,
        parameters: list[float | Literal['min', 'max']],
        disabled: bool = False):
    """Shows a panel to set the ptr_ranges option.

    Args:
        index (int): The index of the option
        parameters (list[float | Literal['min', 'max']]): A list containing the values for the min and max options
        disabled (bool, optional): Indicates whether to disable the setting control. Defaults to False.
    """
    def min_to_str(value: float | Literal['min', 'max']) -> str:
        """Converts the value of the min option to a string.

        Args:
            value (float | Literal['min', 'max']): The input value

        Returns:
            str: The value converted to a string
        """
        if isinstance(value, float):
            if math.isfinite(value):
                return str(value)
            else: 
                return '-inf'
        else:
            return 'min'
    def max_to_str(value: float | Literal['min', 'max']) -> str:
        """Converts the value of the max option to a string.

        Args:
            value (float | Literal['min', 'max']): The input value

        Returns:
            str: The value converted to a string
        """
        if isinstance(value, float):
            if math.isfinite(value):
                return str(value)
            else: 
                return 'inf'
        else:
            return 'max'                
    _, objective, min, max = st.columns((1, 1, 5, 5), vertical_alignment='center')
    key_ptr_range_min: str = f'ptr-range-{index}-min'
    key_ptr_range_max: str = f'ptr-range-{index}-max'
    with objective:
        st.markdown(f'{index + 1}:')
    with min:
        st.text_input(
            key_ptr_range_min,
            value=min_to_str(parameters[0]),
            key=key_ptr_range_min,
            help='number, "-inf", or "min"',
            on_change=on_ptr_range_change,
            kwargs={'control_key': key_ptr_range_min, 'key': 'min', 'parameters': parameters},
            disabled=disabled,
            label_visibility='collapsed')
    with max:
        st.text_input(
            key_ptr_range_max,
            value=max_to_str(parameters[1]),
            key=key_ptr_range_max,
            help='number, "inf", or "max"',
            on_change=on_ptr_range_change,
            kwargs={'control_key': key_ptr_range_max, 'key': 'max', 'parameters': parameters},
            disabled=disabled,
            label_visibility='collapsed')



def control_panel(state: AppState) -> DeltaGenerator:
    """Shows the controller panel.

    Args:
        state (AppState): The application state.

    Returns:
        DeltaGenerator: The placeholder to show the progress bar.
    """
    with st.container(border=True):
        st.markdown(f'#### Controller')
        with st.container():
            left, center, right = st.columns((1, 1, 1))
            with left:
                icon_button('Run', 'play_circle', size=100, key='play_button', button_height=160,
                            disabled=state.working_state == WORKING_STATE_RUN,
                            on_click=on_click_run, kwargs={'state': state}, use_container_width=True)
            with center:
                icon_button('Stop', 'pause_circle', size=100, key='pause_button', button_height=160,
                            disabled=state.working_state != WORKING_STATE_RUN,
                            on_click=on_click_pause, kwargs={'state': state}, use_container_width=True)
            with right:
                icon_button('Reset', 'change_circle', size=100, key='reset_button', button_height=160,
                            disabled=state.working_state != WORKING_STATE_CONFIGURATION,
                            on_click=on_click_reset, kwargs={'state': state}, use_container_width=True)
            
            place_holder: DeltaGenerator = st.empty()
    return place_holder


def cycle_counter_panel() -> DeltaGenerator:
    """Makes a placeholder to show the current cycle number.

    Returns:
        DeltaGenerator: The placeholder to show the count of the cycle.
    """
    with st.container(border=True):
        st.markdown(
            """
            <style>
                .counter-label {{
                    font-family: "Source Sans Pro", sans-serif;
                    font-size: 1.4rem !important;
                    font-weight: 600 !important;
                }}
            </style>
            """,
            unsafe_allow_html=True)        
        st.markdown(f'#### Cycle counter')
        placeholder: DeltaGenerator = st.empty()
    return placeholder


def time_panel() -> dict[str, DeltaGenerator]:
    """Makes placeholders to shows the time progress and remaining time of the processing.

    Returns:
        dict[str, DeltaGenerator]: The placeholders to show the time values.
    """
    placeholders: dict[str, DeltaGenerator] = {}
    with st.container(border=True):
        st.markdown(f'#### Time')
        left_top, right_top = st.columns((1,1))

        with left_top:
            placeholders['step_time'] = st.empty()
            
        with right_top:
            placeholders['cycle_time'] = st.empty()

        left_bottom, _ = st.columns((1,1))

        with left_bottom:
            placeholders['remaining_time'] = st.empty()
    
    return placeholders


def progress(placeholder: DeltaGenerator | list[DeltaGenerator], state: AppState) -> None:
    """Shows the progress of the processing.

    Args:
        placeholder (DeltaGenerator | list[DeltaGenerator]): The placeholder in which the progress is shown.
        state (AppState): The application state.
    """
    assert isinstance(placeholder, DeltaGenerator)
    with placeholder:
        st.progress(state.worker.get_progress())


def cycle_counter(placeholder: DeltaGenerator | list[DeltaGenerator], state: AppState) -> None:
    """Shows the current number of cycles in the processing.

    Args:
        placeholder (DeltaGenerator | list[DeltaGenerator]): The placeholder in which the progress is shown.
        state (AppState): The application state.
    """
    assert isinstance(placeholder, DeltaGenerator)
    num_cycles: Optional[int] = state.worker.get_num_cycles()
    if num_cycles is None:
        num_cycles = state.num_cycles
    with placeholder:
        st.markdown(
            f"""
            <div style="font-size: 1.1rem; font-weight: 600; margin-left: 16px; margin-bottom: 16px;">
                {state.worker.get_current_cycle()} / {num_cycles}
            </div>
            """,
            unsafe_allow_html=True)


def chronograph(placeholder: DeltaGenerator | list[DeltaGenerator], label: str, elapsed: Optional[timedelta]):
    """Shows the time duration as a formatted string.

    Args:
        placeholder (DeltaGenerator | list[DeltaGenerator]): The placeholder in which the time is shown.
        label (AppState): The label for the time string.
        elapsed (Optional[timedelta]): The time duration.
    """
    assert isinstance(placeholder, DeltaGenerator)
    time_str: str
    if elapsed is None:
        time_str = '-- : -- : --'
    else:
        min, sec = divmod(int(elapsed.total_seconds()), 60)
        hr, min = divmod(min, 60)
        time_str = f'{hr}:{min:02d}:{sec:02d}'
    with placeholder:
        st.markdown(
            f"""
            <div style="font-size: 1.1rem; font-weight: 600; margin-bottom: 16px;">
                {label}<br/>
                &nbsp; &nbsp; {time_str}
            </div>
            """,
            unsafe_allow_html=True)


def output_panel(state: AppState) -> DeltaGenerator:
    """Makes a placeholder to show the output of the NIMO processing.

    Args:
        state (AppState): The application state.

    Returns:
        DeltaGenerator: The placeholder to show the output.
    """
    with st.container(border=True):
        title, button = st.columns((9, 1))
        with title:
            st.markdown(f'#### Output')
        with button:
            data = state.worker.stdout() if state.working_state == 'configuration' else ''
            download_output_button(data)
        with st.container(height=300, border=True):
            return st.empty()


@st.fragment
def download_output_button(data: str):
    """Shows the button to download the output.

    Args:
        data (str): The output data.
    """
    st.download_button(
        ':material/file_save:',
        data=data,
        file_name=f'output.txt',
        type='secondary',
        use_container_width=True,
        help='Save output as a text file.',
        key='button-download-output',
        disabled=len(data) == 0)


def output(placeholder: DeltaGenerator | list[DeltaGenerator], state: AppState):
    """Shows the output of the NIMO processing.

    Args:
        placeholder (DeltaGenerator | list[DeltaGenerator]): The placeholder to show the output.
        state (AppState): The application state.
    """
    assert isinstance(placeholder, DeltaGenerator)
    with placeholder:
        st.code(state.worker.stdout(), language=None)


def results_panel(items: list[str], state: AppState) -> dict[str, DeltaGenerator | list[DeltaGenerator]]:
    """Creates placeholders to show result charts.

    Args:
        items (list[str]): The contents.
        state (AppState): The application state.

    Returns:
        dict[str, DeltaGenerator | list[DeltaGenerator]]: The placeholders.
    """
    placeholders: dict[str, DeltaGenerator | list[DeltaGenerator]] = {}
    with st.container(border=True):
        title, button = st.columns((9, 1))
        with title:
            st.markdown(f'#### Results')
        with button:
            data = get_results_archive(state.worker.is_completed())
            download_result_button(data)

        for i, tab in enumerate(st.tabs(items)):
            with tab:
                if items[i] in ['Distribution', 'Phase', 'Phase diagram']:
                    placeholders[items[i]] = st.empty()
                else:
                    placeholders[items[i]] = [st.empty() for _ in range(state.num_objectives)]
    return placeholders


@st.cache_resource(max_entries=1)
def get_results_archive(ready: bool) -> Optional[BytesIO]:
    """Returns the archive data containing the charts and the history data.

    Args:
        ready (bool): A value that represents whether the data is ready or not.
                      This value is used to control the data cacheing.

    Returns:
        Optional[BytesIO]: A BytesIO object in which the archived data is stored.
    """
    if ready == True:
        return get_app_state().worker.get_results_archive()
    else:
        return None


@st.fragment
def download_result_button(data: Optional[DownloadButtonDataType]):
    """Shows the button for downloading the result data.

    Args:
        data (Optional[DownloadButtonDataType]): The archived data of the results.
    """
    disabled: bool = False
    if data is None:
        data = ''
        disabled = True
    st.download_button(
        ':material/file_save:',
        data=data,
        file_name=f'results.zip',
        type='secondary',
        use_container_width=True,
        help='Save results as a ZIP file.',
        key='button_download_results',
        disabled=disabled)


def update_analysis(
        placeholders: dict[str, DeltaGenerator | list[DeltaGenerator]],
        state: AppState):
    """Updates the analysis charts.

    Args:
        placeholders (dict[str, DeltaGenerator | list[DeltaGenerator]]): Placeholders for displaying figures.
        state (AppState): Application state.
    """
    placeholders_objectives = placeholders['Objectives']
    placeholders_best_objective = placeholders['Best objective']
    assert isinstance(placeholders_objectives, list)
    assert isinstance(placeholders_best_objective, list)

    cycle_index: int = state.worker.get_current_cycle()
    if cycle_index <= 1:
        if 'Distribution' in placeholders:
            placeholder_distribution = placeholders['Distribution']
            assert isinstance(placeholder_distribution, DeltaGenerator)
            with placeholder_distribution:
                st.container(height=300, border=False)
        if 'Phase' in placeholders:
            placeholder_phase = placeholders['Phase']
            assert isinstance(placeholder_phase, DeltaGenerator)
            with placeholder_phase:
                st.container(height=300, border=False)
        if 'Phase diagram' in placeholders:
            placeholder_phase_diagram = placeholders['Phase diagram']
            assert isinstance(placeholder_phase_diagram, DeltaGenerator)
            with placeholder_phase_diagram:
                st.container(height=300, border=False)

        for placeholder in placeholders_objectives:
            with placeholder:
                st.container(height=300, border=False)
        for placeholder in placeholders_best_objective:
            with placeholder:
                st.container(height=300, border=False)

    if 'Distribution' in placeholders:
        placeholder_distribution = placeholders['Distribution']
        assert isinstance(placeholder_distribution, DeltaGenerator)
        fig_distribution = state.worker.plot_distribution()
        if fig_distribution is not None:
            with placeholder_distribution:
                st.pyplot(fig_distribution)

    if 'Phase' in placeholders:
        placeholder_phase = placeholders['Phase']
        assert isinstance(placeholder_phase, DeltaGenerator)
        fig_phase = state.worker.plot_phase()
        if fig_phase is not None:
            with placeholder_phase:
                st.pyplot(fig_phase)

    if 'Phase diagram' in placeholders:
        placeholder_phase_diagram = placeholders['Phase diagram']
        assert isinstance(placeholder_phase_diagram, DeltaGenerator)
        fig_phase_diagram = state.worker.plot_phase_diagram()
        if fig_phase_diagram is not None:
            with placeholder_phase_diagram:
                st.pyplot(fig_phase_diagram)

    fig_cycle = state.worker.plot_history_cycle()
    if fig_cycle is not None:
        for placeholder, figure in zip(placeholders_objectives, fig_cycle):
            with placeholder:
                st.pyplot(figure)
    fig_best = state.worker.plot_history_best()
    if fig_best is not None:
        for placeholder, figure in zip(placeholders_best_objective, fig_best):
            with placeholder:
                st.pyplot(figure)

    state.chart_index = cycle_index


def get_app_state() -> AppState:
    """Return the application state object obtained from the session_state of Streamlit.

    Returns:
        AppState: Application state.
    """
    state = st.session_state
    if APP_STATE_KEY not in state:
        state[APP_STATE_KEY] = create_app_state()
    return state[APP_STATE_KEY]


@st.cache_resource
def create_app_state() -> AppState:
    """Create an application state object.
    
    This function is called only once during the runtime of Streamlit.
    The instance of the application state object created at the first call of this function
    is cached by Streamlit, and even if the session changes, the same instance is provided
    when this function is called.

    Returns:
        AppState: Application state.
    """
    return AppState()


def set_event_listener(state: AppState) -> None:
    """Set event listeners to the NIMO worker."""
    if not state.is_event_listener_set:
        state.worker.set_listener('on_suspended', lambda: on_process_suspended(state))
        state.worker.set_listener('on_completed', lambda: on_process_completed(state))
        state.worker.set_listener('on_error', lambda: on_error(state))


def on_control_change(property_key: str, control_key: str, target: Optional[object]=None):
    """Event handler called when the control value is changed.

    Args:
        property_key (str): Property key of AppState class.
        control_key (str): Key of the input control.
    """
    if target is None:
        target = get_app_state()
    value = st.session_state[control_key]
    setattr(target, property_key, value)


def on_select_input_file(path: Path) -> None:
    """Event handler called when the selected input file is changed.

    Args:
        path (Path): Path of the input file.
    """
    state: AppState = get_app_state()
    state.csv_file = path


def on_program_option_change(property_key: str, index: int, control_key: str):
    """Event handler called when the program option is changed.

    Args:
        property_key (str): Property key of AppState class.
        index (int): Index of the program step.
        control_key (str): Key of the selectbox control.
    """
    value = st.session_state[control_key]
    option_list: list = getattr(get_app_state(), property_key)
    for i in range(index, len(option_list)):
        option_list[i] = value


def on_ptr_range_change(
        control_key: str,
        key: Literal['min', 'max'],
        parameters: list[float | Literal['min', 'max', '-inf', 'inf']]):
    """Event handler called when the prt range option is changed.

    Args:
        control_key (str): _description_
        key (Literal['min', 'max']): Parameter key.
        parameters (list[float  |  Literal['min', 'max']]): Parameter list to store the input value.
    """
    if key == 'min':
        value = st.session_state[control_key]
        if value == 'min':
            parameters[0] = 'min'
        elif value == '-inf':
            parameters[0] = -float('inf')
        elif value in ['inf', 'nan']:
            # The string 'inf' is not a valid value for the 'min' variable. 
            parameter_error_dialog('Input a number, "min", or "-inf" for the "min" parameter.')
            st.session_state[control_key] = str(parameters[0])
        else:
            try:
                float_value: float = float(value)
                parameters[0] = float_value
            except:
                # The variable is not updated when an invalid value is provided.
                parameter_error_dialog('Input a number, "min", or "-inf" for the "min" parameter.')
                st.session_state[control_key] = str(parameters[0])
    if key == 'max':
        value = st.session_state[control_key]
        if value == 'max':
            parameters[1] = 'max'
        elif value == 'inf':
            parameters[1] = float('inf')
        elif value in ['-inf', 'nan']:
            # The string '-inf' is not a valid value for the 'max' variable.
            parameter_error_dialog('Input a number, "max", or "inf" for the "max" parameter.')
            st.session_state[control_key] = str(parameters[1])
        else:
            try:
                float_value: float = float(value)
                parameters[1] = float_value
            except:
                # The variable is not updated when an invalid value is provided.
                parameter_error_dialog('Input a number, "max", or "inf" for the "max" parameter.')
                st.session_state[control_key] = str(parameters[1])


def on_select_ai_script(path: Path) -> None:
    """Event handler called when the selected AI script file is changed.

    Args:
        path (Path): The path of the selected AI-algorithm file.
    """
    state: AppState = get_app_state()
    state.custom_ai_algorithm = path


def on_select_robot_script(path: Path) -> None:
    """Event handler called when the selected robot-operation file is changed.

    Args:
        path (Path): The path of the selected robot-operation file.
    """
    state: AppState = get_app_state()
    state.custom_robotic_system = path


def on_select_analysis_script(path: Path) -> None:
    """Event handler called when the selected analysis script file is changed.

    Args:
        path (Path): The path of the selected analysis script file.
    """
    state: AppState = get_app_state()
    state.custom_analysis = path


def on_click_run(state: AppState) -> None:
    """Click event handler of the run button.

    Args:
        state (AppState): Application state.
    """
    if state.working_state == WORKING_STATE_CONFIGURATION:
        parameters = state.get_parameters()
        error: Optional[str] = parameters.check()
        if error:
            parameter_error_dialog(error)
        else:
            state.chart_index = 0
            state.working_state = WORKING_STATE_RUN
            state.worker.run(parameters)
    elif state.working_state in [WORKING_STATE_SUSPENDING, WORKING_STATE_SUSPENDED] :
        state.working_state = WORKING_STATE_RUN
        state.worker.resume()


def on_click_pause(state: AppState) -> None:
    """Click event handler of the pause button.

    Args:
        state (AppState): Application state.
    """
    print('on_click_pause')
    if state.working_state == WORKING_STATE_RUN:
        state.working_state = WORKING_STATE_SUSPENDING
        print('Call worker.suspend()')
        state.worker.suspend()


def on_click_reset(state: AppState) -> None:
    """Click event handler of the reset button.

    Args:
        state (AppState): Application state.
    """
    if state.worker.is_completed():
        reset_confirmation_dialog(state)
    else:
        state.reset_parameters()


def on_process_suspended(state: AppState) -> None:
    """Event handler called when the NIMO process has been suspended.

    Args:
        state (AppState): Application state.
    """
    print('on_process_suspended')
    state.working_state = WORKING_STATE_SUSPENDED


def on_process_completed(state: AppState) -> None:
    """Event handler called when the NIMO process has been completed.

    Args:
        state (AppState): Application state.
    """
    state.working_state = WORKING_STATE_CONFIGURATION
    completed_dialog()


def on_error(state: AppState) -> None:
    """Event handler called when the NIMO process has been interrupted by some error.

    Args:
        state (AppState): Application state.
    """
    state.working_state = WORKING_STATE_CONFIGURATION
    error_message: Optional[str] = state.worker.get_last_exception()
    if error_message is not None:
        worker_error_dialog(error_message)


def on_click_exit() -> None:
    """Stop the streamlit server process."""
    pid: int = os.getpid()
    os.kill(pid, signal.SIGKILL)


def update_placeholders(
        placeholders: dict[str, DeltaGenerator | list[DeltaGenerator]],
        state: AppState) -> None:
    """Update views.

    Args:
        placeholders (dict[str, DeltaGenerator | list[DeltaGenerator]]): Placeholders to be updated.
        state (AppState): Application state.
    """
    progress(placeholders['progress'], state)
    cycle_counter(placeholders['cycle_counter'], state)
    chronograph(placeholders['step_time'], 'Time for AI algorithm', state.worker.get_ai_selection_time())
    chronograph(placeholders['cycle_time'], 'Time for one cycle', state.worker.get_cycle_time())
    chronograph(placeholders['remaining_time'], 'Estimated remaining time', state.worker.get_remaining_time())
    output(placeholders['output'], state)
    

def do_observer_loop(
        placeholders: dict[str, DeltaGenerator | list[DeltaGenerator]],
        state: AppState):
    """Check the state of NimoWorker, and update displays.

    Args:
        placeholders (dict[str, DeltaGenerator]): Placeholders to be updated.
        state (AppState): Application state object.
    """
    while state.working_state in [WORKING_STATE_RUN, WORKING_STATE_SUSPENDING]:
        state.worker.update_state()
        update_placeholders(placeholders, state)
        if state.chart_index != state.worker.get_current_cycle():
            update_analysis(placeholders, state)
        time.sleep(1.0)
