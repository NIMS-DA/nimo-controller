"""NIMO background process worker"""

from datetime import datetime, timedelta
from io import BytesIO, StringIO
from multiprocessing import Process, Queue
import os
import sys
import time
from types import ModuleType
from typing import Any, Callable, Iterable, Literal, Optional, TextIO, TypeAlias
from zipfile import ZIP_DEFLATED, ZipFile

from matplotlib import pyplot as plt
from matplotlib.figure import Figure
import nimo
from nimo_gui.backend.queuestream import QueueStream
from nimo_gui.constants import AI_ALGORITHM_ORIGINAL, ROBOTIC_SYSTEM_ORIGINAL
from nimo_gui.visualization import plot_distribution, plot_history, plot_phase_diagram
from nimo_gui.backend.parameters import NimoParameters

# This is a setting for the test using the mock of NIMO.
# from tests.backend import mock as nimo


GET_STEP_COMMAND: Literal['get_step'] = 'get_step'
GET_START_TIME_COMMAND: Literal['get_start_time'] = 'get_start_time'

SUSPEND_PROCESS_COMMAND: Literal['pause'] = 'pause'
RESUME_PROCESS_COMMAND: Literal['resume'] = 'resume'

TAG_STEP_START: Literal['step_start'] = 'step_start'
TAG_STEP_END: Literal['step_end'] = 'step_end'
TAG_PROCESS_SUSPENDED: Literal['suspended'] = 'suspended'
TAG_ERROR: Literal['error'] = 'error'

STEP_IDLING: Literal['idle'] = 'idle'
STEP_AI_SELECTION: Literal['ai_selection'] = 'ai_selection'
STEP_ROBOT_OPERATION: Literal['robot_operation'] = 'robot_operation'
STEP_ANALYSIS: Literal['analysis'] = 'analysis'
STEP_COMPLETED: Literal['completed'] = 'completed'

WorkerState: TypeAlias = Literal['idle', 'ai_selection', 'robot_operation', 'analysis', 'completed']

CYCLE: Literal['cycle'] = 'cycle'

EVENT_ON_COMPLETED: Literal['on_completed'] = 'on_completed'
EVENT_ON_STEP_COMPLETED: Literal['on_step_completed'] = 'on_step_completed'
EVENT_ON_SUSPENDED: Literal['on_suspended'] = 'on_suspended'
EVENT_ON_ERROR: Literal['on_error'] = 'on_error'

DUMMY_TIMESTAMP: datetime = datetime.fromtimestamp(0)

NimoWorkerEvent: TypeAlias = Literal['on_completed', 'on_step_completed', 'on_suspended', 'on_error']


class NimoWorker:
    """A class for running and managing the NIMO module as a subprocess."""

    def __init__(self) -> None:
        """The function called when the instance is being created."""
        self._process: Optional[Process] = None
        self._sender_queue: Queue = Queue()
        self._receiver_queue: Queue = Queue()
        self._stdout_queue: Queue = Queue()
        self._stdout_log: str = ''
        self._is_available: bool = True
        self._is_running: bool = False
        self._is_suspending: bool = False
        self._is_completed: bool = False
        self._progress: int = 0
        self._error_message: Optional[str] = None

        self._parameters: Optional[NimoParameters] = None

        self._step_durations: dict[str, list[timedelta]] = \
            { step: [] for step in [STEP_AI_SELECTION, STEP_ROBOT_OPERATION, STEP_ANALYSIS] }
        
        self._average_step_durations: dict[str, Optional[timedelta]] = \
            { step: None for step in [STEP_AI_SELECTION, STEP_ROBOT_OPERATION, STEP_ANALYSIS, CYCLE] }
        self._current_step: WorkerState = STEP_IDLING
        self._current_cycle: int = 0

        self._start_time: Optional[datetime] = None
        self._end_time: Optional[datetime] = None
        self._step_start_time: datetime = DUMMY_TIMESTAMP
        self._offset: timedelta = timedelta(0)

        self._history_index: int = 0
        self._history: Optional[list[Any]] = None

        self._is_pdc_used: Optional[bool] = None

        self._plot_data_distribution: list[BytesIO] = []
        self._plot_data_phase: list[BytesIO] = []
        self._plot_data_phase_diagram: list[BytesIO] = []
        self._plot_data_objectives: list[BytesIO] = []
        self._plot_data_best_objective: list[BytesIO] = []

        self._plot_distribution: Optional[Figure] = None
        self._plot_phase: Optional[Figure] = None
        self._plot_phase_diagram: Optional[Figure] = None
        self._plot_objectives: Optional[list[Figure]] = None
        self._plot_best_objective: Optional[list[Figure]] = None

        self._event_listeners: dict[str, Optional[Callable[[], None]]] = \
            { step: None for step in [EVENT_ON_SUSPENDED, EVENT_ON_STEP_COMPLETED, EVENT_ON_COMPLETED, EVENT_ON_ERROR] }

    def __del__(self):
        """"The function called when the instance is destroyed."""
        if self._process is not None and self._process.is_alive():
            self._process.kill()
            self._sender_queue.close()
            self._receiver_queue.close()
            self._stdout_queue.close()
            self._event_listeners.clear()
            self._close_figs()

    def _close_figs(self):
        """"Closes all figures and releases the memory."""
        if self._plot_distribution is not None:
            plt.close(self._plot_distribution)
            self._plot_distribution = None
        
        if self._plot_phase is not None:
            plt.close(self._plot_phase)
            self._plot_phase = None
        
        if self._plot_phase_diagram is not None:
            plt.close(self._plot_phase_diagram)
            self._plot_phase_diagram = None
        
        if self._plot_objectives is not None:
            for fig in self._plot_objectives:
                plt.close(fig)
            self._plot_objectives.clear()
            self._plot_objectives = None
        
        if self._plot_best_objective is not None:
            for fig in self._plot_best_objective:
                plt.close(fig)
            self._plot_best_objective.clear()
            self._plot_best_objective = None
        
        for plot_data_distribution in self._plot_data_distribution:
            plot_data_distribution.close()
        self._plot_data_distribution.clear()
        
        for plot_data_phase in self._plot_data_phase:
            plot_data_phase.close()
        self._plot_data_phase.clear()
        
        for plot_data_phase_diagram in self._plot_data_phase_diagram:
            plot_data_phase_diagram.close()
        self._plot_data_phase_diagram.clear()

        if self._plot_data_objectives is not None:
            for plot_data_objectives in self._plot_data_objectives:
                plot_data_objectives.close()
            self._plot_data_objectives.clear()

        if self._plot_data_best_objective is not None:
            for plot_data_best_objective in self._plot_data_best_objective:
                plot_data_best_objective.close()
            self._plot_data_best_objective.clear()

    def is_pdc_used(self) -> Optional[bool]:
        """Returns whether the PDC algorithm is used in the program."""
        return self._is_pdc_used
    
    def stdout(self) -> str:
        """Get the standard output of the NIMO background process.

        Returns:
            str: Standard output of the NIMO process
        """
        new_lines: list[str] = []
        while not self._stdout_queue.empty():
            res = self._stdout_queue.get_nowait()
            if isinstance(res, str):
                new_lines.append(res)
        if 0 < len(new_lines):
            self._stdout_log += '\n'.join(new_lines)
            self._stdout_log += '\n'

        return self._stdout_log
    
    def set_listener(self, event: NimoWorkerEvent, listener: Callable[[], None]) -> None:
        """Set an event listener.

        Args:
            event (NimoWorkerEvent): Event type.
            listener (Callable[[], None]): Event listener.
        """
        self._event_listeners[event] = listener
    
    def reset_states(self) -> None:
        """Reset the state of the worker."""
        if self._is_running:
            print('The worker reset is refused because the worker is currently running.')
            return

        self._is_suspending = False
        self._is_completed = False
        self._error_message = None

        self._parameters = None
        self._stdout_log: str = ''
        self._progress = 0

        self._step_durations = \
            { step: [] for step in [STEP_AI_SELECTION, STEP_ROBOT_OPERATION, STEP_ANALYSIS] }
        
        self._average_step_durations = \
            { step: None for step in [STEP_AI_SELECTION, STEP_ROBOT_OPERATION, STEP_ANALYSIS, CYCLE] }
        self._current_step = STEP_IDLING
        self._current_cycle = 0

        self._start_time = None
        self._end_time = None
        self._step_start_time = DUMMY_TIMESTAMP
        self._offset = timedelta(0)

        self._history_index = 0
        self._history = None
        self._close_figs()
        print('The state has been reset.')

    def run(self, parameters: NimoParameters) -> None:
        """Start the NIMO background process.

        Args:
            parameters (NimoParameters): Parameter settings of NIMO.
        """
        if self._is_running:
            assert self._start_time is not None
            print(f'NIMO has already been running since {self._start_time.strftime(r"%Y/%m/%d %H:%M:%S")}')

        # Initialize the fields.
        self.reset_states()
        
        if self._process is None and not self._is_running:
            kwargs = {
                'parameters': parameters,
                'input': self._sender_queue,
                'output': self._receiver_queue,
                'stdout': self._stdout_queue
            }
            self._parameters = parameters
            self._is_pdc_used = parameters.is_pdc_used()
            self._is_running = True
            self._process = Process(target=NimoWorker._worker, kwargs=kwargs)
            self._process.start()
    
    def suspend(self) -> None:
        """Suspend the processing."""
        if self._is_running and not self._is_suspending:
            self._is_suspending = True
            self._sender_queue.put(SUSPEND_PROCESS_COMMAND)
    
    def resume(self) -> None:
        """Resume the processing."""
        if not self._is_running:
            self._end_time = self._calc_end_time(datetime.now())
            self._sender_queue.put(RESUME_PROCESS_COMMAND)
            self._is_running = True
    
    def _check_command(self):
        """Check the command message from the subprocess and process it."""
        while True:
            if self._receiver_queue.empty():
                break

            command = self._receiver_queue.get_nowait()
            if command is None:
                continue

            assert isinstance(command, tuple)

            tag, data = command
            assert isinstance(tag, str)

            if tag == TAG_STEP_START:
                step, start_time = data
                if self._current_cycle == 0 and self._current_step == STEP_IDLING:
                    assert step == STEP_AI_SELECTION
                    self._start_time = start_time
                    print(f'[NIMO] start: {start_time}')
                
                if step == STEP_AI_SELECTION:
                    self._current_cycle += 1
                
                # Update the states of the NIMO worker.
                self._step_start_time = start_time
                self._current_step = step
                self._end_time = self._calc_end_time(start_time)
            
            elif tag == TAG_STEP_END:
                step, end_time, history = data
                elapsed_time: timedelta = end_time - self._step_start_time
                self._offset += elapsed_time
                if step != STEP_COMPLETED:
                    self._progress += 1

                if step in self._step_durations:
                    self._step_durations[step].append(elapsed_time)
                    self._average_step_durations[step] = self._average(self._step_durations[step])

                if step == STEP_ANALYSIS:
                    self._history = history
                    self._history_index += 1
                    self._average_step_durations[CYCLE] = self._calc_cycle_average()
                    self._prepare_figures()

                # Raise the "on_step_completed" event.
                self._call_listener(EVENT_ON_STEP_COMPLETED)
            
                if step == STEP_COMPLETED:
                    print(f'[NIMO] completed: {end_time}')
                    self._is_running = False
                    self._is_completed = True
                    assert self._process is not None
                    self._process.join(10.)
                    self._process = None
                    # Raise the "on_completed" event.
                    self._call_listener(EVENT_ON_COMPLETED)
            
            elif tag == TAG_PROCESS_SUSPENDED:
                self._is_running = False
                self._is_suspending = False
                # Raise the "on_suspended" event.
                self._call_listener(EVENT_ON_SUSPENDED)
            
            elif tag == TAG_ERROR:
                self._is_running = False
                assert self._process is not None
                self._process.join(10.)
                self._process = None
                assert isinstance(data, tuple) and len(data) == 2
                error_message, _ = data
                assert isinstance(error_message, str)
                self._error_message = error_message
                print(self._error_message)
                # Raise the "on_error" event.
                self._call_listener(EVENT_ON_ERROR)
    
    def is_completed(self) -> bool:
        """Returns whether the processing is complete.

        Returns:
            bool: Returns True if the NIMO background process has been completed; otherwise, False.
        """
        return self._is_completed
    
    def get_ai_selection_time(self) -> Optional[timedelta]:
        """Returns the average time duration fot the AI selection step.

        Returns:
            Optional[timedelta]: Average time duration fot the AI selection step.
        """
        if self._average_step_durations[STEP_AI_SELECTION] is None:
            if self._is_running:
                if self._step_start_time == DUMMY_TIMESTAMP:
                    return None
                else:
                    return datetime.now() - self._step_start_time
            else:
                return None
        else:
            return self._average_step_durations[STEP_AI_SELECTION]
        
    def get_cycle_time(self) -> Optional[timedelta]:
        """Returns the average time duration for the one cycle of the processing.

        Returns:
            Optional[timedelta]: Average time duration for the one cycle of the processing.
        """
        if self._average_step_durations[CYCLE] is None:
            if self._is_running:
                if self._step_start_time == DUMMY_TIMESTAMP:
                    return None
                else:
                    return datetime.now() - self._step_start_time + self._offset
            else:
                return None
        else:
            return self._average_step_durations[CYCLE]
    
    def get_remaining_time(self) -> Optional[timedelta]:
        """Returns the remaining time of the processing.

        Returns:
            Optional[timedelta]: Remaining time of the processing.
        """
        if self._end_time is None:
            return None
        else:
            now = datetime.now()
            if now < self._end_time:
                return self._end_time - now
            else:
                return timedelta(0)
    
    def get_num_cycles(self) -> Optional[int]:
        """Returns the number of the cycles to run.

        Returns:
            Optional[int]: Number of the cycles.
        """
        if self._parameters:
            return self._parameters.num_cycles
        else:
            return None
    
    def get_current_cycle(self) -> int:
        """Returns the current cycle index.

        Returns:
            int: Current cycle index (1-based).
        """
        return self._current_cycle
        
    def get_progress(self) -> float:
        """Returns the progress ratio.

        Returns:
            float: The progress ratio.
        """
        if self._parameters is None:
            return 0.
        else:
            return self._progress / (self._parameters.num_cycles * 3)
    
    def get_last_exception(self) -> Optional[str]:
        """Returns the error message of the exception that occurred in the NIMO process.

        Returns:
            Optional[str]: Error message.
        """
        return self._error_message
    
    def _average(self, time_duration_list: Iterable[timedelta]) -> timedelta:
        """Calculate the average of the given time durations.

        Args:
            time_duration_list (Iterable[timedelta]): Time durations.

        Returns:
            timedelta: The average time duration.
        """
        sum: timedelta = timedelta(0)
        count: int = 0
        for time_span in time_duration_list:
            sum += time_span
            count += 1
        return sum / count if 0 < count else timedelta(0)
    
    def _calc_cycle_average(self) -> timedelta:
        """Calculate the average time duration to process a cycle.

        Returns:
            timedelta: The average time duration to process a cycle.
        """
        sum: timedelta = timedelta(0)
        count: int = min(len(list) for list in self._step_durations.values())
        for time_span_list in self._step_durations.values():
            for i in range(count):
                sum += time_span_list[i]
        return sum / count if 0 < count else timedelta(0)
    
    def _calc_end_time(self, start_time: datetime) -> Optional[datetime]:
        """Calculate the time at which the processing will finish.

        Args:
            start_time (datetime): The time at which the processing started.

        Returns:
            Optional[datetime]: The time at which the processing will finish.
        """
        if self._current_cycle <= 1:
            return None
        assert self._parameters is not None

        remaining_count = {
            step: self._parameters.num_cycles - len(self._step_durations[step])
            for step in [STEP_AI_SELECTION, STEP_ROBOT_OPERATION, STEP_ANALYSIS]
        }
        
        end_time: datetime = start_time
        for step in [STEP_AI_SELECTION, STEP_ROBOT_OPERATION, STEP_ANALYSIS]:
            duration: Optional[timedelta] = self._average_step_durations[step]
            assert duration is not None
            end_time += duration * remaining_count[step]
        return end_time        
    
    def update_state(self) -> None:
        """Update the state parameters."""
        self._check_command()
    
    def get_history(self) -> Optional[list[Any]]:
        """Returns the processing history.

        Returns:
            Optional[list[Any]]: History data of the processing.
        """
        return self._history
    
    def _prepare_figures(self) ->None:
        if self._parameters is None:
            return
        
        if self._is_pdc_used == False:
            if self._plot_distribution is not None:
                plt.close(self._plot_distribution)
            self._plot_distribution = plot_distribution.plot(
                input_file=str(self._parameters.candidates_file),
                num_objectives=self._parameters.num_objectives)
            if self._plot_distribution is not None:
                self._plot_data_distribution.append(NimoWorker._fig_to_data(self._plot_distribution))
        elif self._is_pdc_used == True:
            if self._plot_phase is not None:
                plt.close(self._plot_phase)
            if self._plot_phase_diagram is not None:
                plt.close(self._plot_phase_diagram)
            figs = plot_phase_diagram.plot(input_file=str(self._parameters.candidates_file))
            if len(figs) == 2:
                self._plot_phase = figs[0]
                self._plot_data_phase.append(NimoWorker._fig_to_data(self._plot_phase))
                self._plot_phase_diagram = figs[1]
                self._plot_data_phase_diagram.append(NimoWorker._fig_to_data(self._plot_phase_diagram))

        if self._plot_objectives is not None:
            for fig in self._plot_objectives:
                plt.close(fig)
            self._plot_objectives.clear()
        self._plot_objectives = plot_history.cycle(
            input_file=self._history,
            num_cycles=self._parameters.num_cycles)
        if self._current_cycle == self._parameters.num_cycles:
            for fig in self._plot_objectives:
                self._plot_data_objectives.append(NimoWorker._fig_to_data(fig))

        if self._plot_best_objective is not None:
            for fig in self._plot_best_objective:
                plt.close(fig)
            self._plot_best_objective.clear()
        self._plot_best_objective = plot_history.best(
            input_file = self._history,
            num_cycles = self._parameters.num_cycles)
        if self._current_cycle == self._parameters.num_cycles:
            for fig in self._plot_best_objective:
                self._plot_data_best_objective.append(NimoWorker._fig_to_data(fig))
    
    @staticmethod
    def _fig_to_data(fig: Figure) -> BytesIO:
        """Saves the Figure object to  an image data.

        Args:
            fig (Figure): Figure.

        Returns:
            BytesIO: Image data.
        """
        data: BytesIO = BytesIO()
        fig.savefig(data, format='png')
        return data
    
    def plot_distribution(self) -> Optional[Figure]:
        """Returns the distribution chart.

        Returns:
            Optional[Figure]: The Figure object that represents the distribution chart.
        """
        return self._plot_distribution
    
    def plot_phase(self) -> Optional[Figure]:
        """Returns the phase chart.

        Returns:
            Optional[Figure]: The Figure object that represents the phase chart.
        """
        return self._plot_phase
    
    def plot_phase_diagram(self) -> Optional[Figure]:
        """Returns the phase diagram.

        Returns:
            Optional[Figure]: The Figure object that represents the phase diagram.
        """
        return self._plot_phase_diagram
    
    def plot_history_cycle(self) -> Optional[list[Figure]]:
        """Returns the cycle history chart.

        Returns:
            Optional[Figure]: The Figure object that represents the cycle history chart.
        """
        return self._plot_objectives
        
    def plot_history_best(self) -> Optional[list[Figure]]:
        """Returns the distribution chart.

        Returns:
            Optional[Figure]: The Figure object that represents the best objectives at each cycle.
        """
        return self._plot_best_objective
        
    def get_results_archive(self) -> Optional[BytesIO]:
        """Returns archived data containing the results plot and the history data.

        Returns:
            Optional[BytesIO]: Archived data
        """
        if self._parameters is None:
            return None
        archive: BytesIO = BytesIO()
        with ZipFile(archive, 'w', compression=ZIP_DEFLATED) as zip:
            for i, plot_data in enumerate(self._plot_data_distribution):
                zip.writestr(f'distribution_cycle{i + 1}.png', plot_data.getbuffer())
            for i, plot_data in enumerate(self._plot_data_phase):
                zip.writestr(f'phase_cycle{i + 1}.png', plot_data.getbuffer())
            for i, plot_data in enumerate(self._plot_data_phase_diagram):
                zip.writestr(f'phase_diagram_cycle{i + 1}.png', plot_data.getbuffer())
            for i, plot_data in enumerate(self._plot_data_objectives):
                zip.writestr(f'history_step_{i + 1}.png', plot_data.getbuffer())
            for i, plot_data in enumerate(self._plot_data_best_objective):
                zip.writestr(f'history_best_{i + 1}.png', plot_data.getbuffer())
            if self._history is not None:
                zip.writestr('history.txt', NimoWorker.history_to_csv(self._history))
        return archive
    
    @staticmethod
    def history_to_csv(history: list[Any]) -> str:
        """Returns CSV-formatted history data.

        Args:
            history (list[Any]): History data.

        Returns:
            str: CSV-formatted history data.
        """
        import csv
        assert isinstance(history, list)
        with StringIO() as buffer:
            writer = csv.writer(buffer)
            for record in history:
                assert len(record) == 3
                index: int = record[0]
                x_train: list = record[1]
                t_train: list = record[2]
                row: list[Any] = [index] + x_train + t_train
                writer.writerow(row)
            return buffer.getvalue()
    
    def _call_listener(self, event: str) -> None:
        """Calls an event listener.

        Args:
            event (str): Event name.
        """
        listener: Optional[Callable[[], None]] = self._event_listeners[event]
        if listener:
            listener()

    @staticmethod
    def _worker(
            parameters: NimoParameters,
            input: Queue,
            output: Queue,
            stdout: Queue) -> None:
        """The entry point to run NIMO in the subprocess.

        The subprocess communicates with the main process using queues.

        Args:
            parameters (NimoParameters): The parameters for NIMO.
            input (Queue): The queue used for the input to the NIMO subprocess.
            output (Queue): The queue used for the output from the NIMO subprocess.
            stdout (Queue): The queue used for the stdout redirection.
        """
        pause_flag: bool = False

        def timestamp() -> str:
            """Returns the formatted text of the current timestamp.

            Returns:
                str: The formatted text of the current timestamp.
            """
            return datetime.now().strftime(r'%Y/%m/%d %H:%M:%S')
        
        def check_command() -> None:
            """Check the command queue and process the recieved command."""
            nonlocal pause_flag
            while True:
                if input.empty():
                    return
                command = input.get_nowait()
                if command == SUSPEND_PROCESS_COMMAND:
                    pause_flag = True
                elif command == RESUME_PROCESS_COMMAND:
                    pause_flag = False
        
        def pause_process() -> None:
            """Suspend the processing until the flag is set."""
            nonlocal pause_flag
            if pause_flag:
                output.put((TAG_PROCESS_SUSPENDED, datetime.now()))
            while pause_flag:
                time.sleep(1)
                check_command()
        
        def process_command() -> None:
            """Process the recieved messages."""
            # Check the message queue.
            check_command()
                
            # Pause operation if the flag is set.
            pause_process()
        
        def step_start(step: str) -> None:
            """Starts the next step.

            Args:
                step (str): The name of the next step to be executed.
            """
            output.put((TAG_STEP_START, (step, datetime.now())))
            
        def step_end(step: str, history: list[Any]) -> None:
            """Ends the current step.

            Args:
                step (str): The name of the current step that has been completed.
            """
            output.put((TAG_STEP_END, (step, datetime.now(), history)))
        
        def report_error(ex: Exception) -> None:
            """Report the error information to the main process.

            Args:
                ex (Exception): The Exception object raised.
            """
            output.put((TAG_ERROR, (str(ex.with_traceback(sys.exc_info()[2])), datetime.now())))
                    
        def run() -> None:
            """Starts the NIMO processing."""
            num_cycles: int = parameters.num_cycles
            num_objectives: int = parameters.num_objectives
            candidates_file: str = str(parameters.candidates_file)
            proposals_file: str = str(parameters.proposals_file)
            input_folder: str = str(parameters.input_folder)
            output_folder: str = str(parameters.output_folder)
            ai_algorithm_options: dict[str, dict[str, Any]] = parameters.ai_algorithm_options
            ai_tool_original: Optional[ModuleType] = None
            preparation_input_original: Optional[ModuleType] = None
            analysis_output_original: Optional[ModuleType] = None
            
            # Set the current working directory to change the destination of the output files.
            os.chdir(input_folder)

            # Create a history object to store results.
            res_history: list[Any] = nimo.history(
                input_file = candidates_file,
                num_objectives = num_objectives)
            
            for i in range(num_cycles):
                print(f'[NIMO_GUI][{timestamp()}] Start cycle-{i + 1}.')
                ai_algorithm, num_proposals, robot_operation = parameters.program[i]

                step_start(STEP_AI_SELECTION)
                if ai_algorithm != AI_ALGORITHM_ORIGINAL:
                    options: dict[str, Any] = ai_algorithm_options[ai_algorithm] \
                        if ai_algorithm in ai_algorithm_options else {}
                    nimo.selection(
                        method = ai_algorithm,
                        input_file = candidates_file,
                        output_file = proposals_file,
                        num_objectives = num_objectives,
                        num_proposals = num_proposals,
                        **options)
                else:
                    if ai_tool_original is None:
                        ai_tool_original = parameters.get_custom_ai_algorithm_module()
                    assert ai_tool_original is not None
                    assert hasattr(ai_tool_original, 'ORIGINAL')
                    ai_tool_original.ORIGINAL(
                        input_file = candidates_file,
                        output_file = proposals_file,
                        num_objectives = num_objectives,
                        num_proposals = num_proposals).select()
                step_end(STEP_AI_SELECTION, res_history)
                
                # Process the commands received from the main process.
                process_command()
                
                step_start(STEP_ROBOT_OPERATION)
                if robot_operation != ROBOTIC_SYSTEM_ORIGINAL:
                    nimo.preparation_input(
                        machine=robot_operation,
                        input_file=proposals_file,
                        input_folder=input_folder)
                else:
                    if preparation_input_original is None:
                        preparation_input_original = parameters.get_custom_robotic_system_module()
                    assert preparation_input_original is not None
                    preparation_input_original.ORIGINAL(
                        input_file = proposals_file,
                        input_folder = input_folder).perform()
                step_end(STEP_ROBOT_OPERATION, res_history)
                
                # Process the commands received from the main process.
                process_command()

                # Perform the analysis.
                step_start(STEP_ANALYSIS)
                if robot_operation != ROBOTIC_SYSTEM_ORIGINAL:
                    nimo.analysis_output(
                        machine = robot_operation,
                        input_file = proposals_file,
                        output_file = candidates_file,
                        num_objectives = parameters.num_objectives,
                        output_folder = output_folder)
                else:
                    if analysis_output_original is None:
                        analysis_output_original = parameters.get_custom_analysis_module()
                    assert analysis_output_original is not None
                    analysis_output_original.ORIGINAL(
                        input_file = proposals_file,
                        output_file = candidates_file,
                        num_objectives = num_objectives,
                        output_folder = output_folder).perform()
                
                # Append the results to the history object.
                res_history = nimo.history(
                    input_file = candidates_file,
                    num_objectives = num_objectives,
                    itt = i,
                    history_file = res_history)
                
                step_end(STEP_ANALYSIS, res_history)

                # Check whether the loop has completed the number of iterations.
                if i == num_cycles - 1:
                    break

                # Process the commands received from the main process.
                process_command()

            step_end(STEP_COMPLETED, res_history)
            print(f'[NIMO_GUI][{timestamp()}] Complete.')
            
        queue_stream: QueueStream = QueueStream(stdout) 
        stored_stdout: TextIO = sys.stdout
        sys.stdout = queue_stream
        try:
            run()
        except Exception as ex:
            report_error(ex)
        finally:
            sys.stdout = stored_stdout
