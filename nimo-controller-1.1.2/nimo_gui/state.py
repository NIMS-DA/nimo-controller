"""Data class of the application state"""

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from nimo_gui.algorithm_options import AiAlgorithmOptions
from nimo_gui.backend.parameters import NimoParameters
from nimo_gui.backend.worker import NimoWorker
from nimo_gui.constants import (AI_ALGORITHM_BLOX, AI_ALGORITHM_PDC, AI_ALGORITHM_PHYSBO, AI_ALGORITHM_PTR,
                                AI_ALGORITHM_RE, AI_ALGORITHM_SLESA, AI_ALGORITHMS, WorkingState)


@dataclass
class AppState:
    # Resources
    worker: NimoWorker = field(default_factory=NimoWorker)

    # Application state parameters
    working_state: WorkingState = 'configuration'
    file_browser_visible: bool = False
    is_event_listener_set: bool = False
    chart_index: int = -1

    # Parameters for NIMO
    num_objectives: int = 1
    num_cycles: int = 1
    csv_file: Optional[Path] = None
    custom_ai_algorithm: Optional[Path] = None
    custom_robotic_system: Optional[Path] = None
    custom_analysis: Optional[Path] = None
    ai_algorithms: list[str] = field(default_factory=list)
    num_proposals: list[int] = field(default_factory=list)
    robotic_systems: list[str] = field(default_factory=list)
    option_ai_algorithm: str = AI_ALGORITHM_RE
    ai_algorithm_options: AiAlgorithmOptions = field(default_factory=AiAlgorithmOptions)

    @property
    def selected_ai_ai_algorithms(self) -> list[str]:
        return [algorithm for algorithm in AI_ALGORITHMS if algorithm in self.ai_algorithms]

    def reset_parameters(self) -> None:
        """Resets the parameter settings."""
        self.num_objectives = 1
        self.num_cycles = 1
        self.csv_file = None
        self.custom_ai_algorithm = None
        self.custom_robotic_system = None
        self.custom_analysis = None
        self.ai_algorithms.clear()
        self.num_proposals.clear()
        self.robotic_systems.clear()
        self.worker.reset_states()

    def get_parameters(self) -> NimoParameters:
        """Creates a parameter object for NIMO from current settings.

        Returns:
            NimoParameters: A parameter object for NIMO.
        """
        assert self.csv_file is not None
        assert self.num_cycles <= len(self.ai_algorithms)
        assert self.num_cycles <= len(self.robotic_systems)
        input_folder: Path = self.csv_file.parent
        return NimoParameters(
            num_objectives=self.num_objectives,
            num_cycles=self.num_cycles,
            input_folder=input_folder,
            output_folder=input_folder,
            candidates_file=self.csv_file,
            proposals_file=input_folder.joinpath('proposals.csv'),
            program=[(self.ai_algorithms[i], self.num_proposals[i], self.robotic_systems[i]) for i in range(self.num_cycles)],
            ai_algorithm_options=self._get_ai_option_dict(),
            custom_ai_algorithm=self.custom_ai_algorithm,
            custom_robotic_system=self.custom_robotic_system,
            custom_analysis=self.custom_analysis)
    
    def is_pdc_used(self) -> bool:
        """Returns whether the PDC algorithm is used in the program.

        Returns:
            bool: True if the PDC algorithm is used in the program; otherwise, False.
        """
        res: Optional[bool] = self.worker.is_pdc_used()
        if res is not None:
            return res
        else:
            for ai_algorithm in self.ai_algorithms:
                if ai_algorithm == AI_ALGORITHM_PDC:
                    return True
            return False

    def _get_ai_option_dict(self) -> dict[str, dict[str, Any]]:
        """Returns a dictionary representing the AI algorithm options.
        
        Returns:
            dict[str, dict[str, Any]]: A dictionary containing the AI algorithm options.
        """
        option_dict: dict[str, dict[str, Any]] = {}
        for algorithm in set(self.ai_algorithms[:self.num_cycles]):
            if algorithm == AI_ALGORITHM_RE:
                option_dict[AI_ALGORITHM_RE] = asdict(self.ai_algorithm_options.re)
            elif algorithm == AI_ALGORITHM_PHYSBO:
                if 1 < self.num_objectives: 
                    option_dict[AI_ALGORITHM_PHYSBO] = asdict(self.ai_algorithm_options.physbo_multi)
                else:
                    option_dict[AI_ALGORITHM_PHYSBO] = asdict(self.ai_algorithm_options.physbo_single)
            elif algorithm == AI_ALGORITHM_PDC:
                option_dict[AI_ALGORITHM_PDC] = asdict(self.ai_algorithm_options.pdc)
            elif algorithm == AI_ALGORITHM_BLOX:
                option_dict[AI_ALGORITHM_BLOX] = asdict(self.ai_algorithm_options.blox)
            elif algorithm == AI_ALGORITHM_PTR:
                option_dict[AI_ALGORITHM_PTR] = self.ai_algorithm_options.ptr.to_dict(self.num_objectives)
            elif algorithm == AI_ALGORITHM_SLESA:
                option_dict[AI_ALGORITHM_SLESA] = asdict(self.ai_algorithm_options.slesa)
            else:
                print(f'[WARN] Unknown AI algorithm ("{algorithm}") appeared.')
        return option_dict
