"""Data class of the parameters used in NIMO"""

from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any, Literal, Optional

from nimo_gui.constants import AI_ALGORITHM_ORIGINAL, AI_ALGORITHM_PDC, ROBOTIC_SYSTEM_ORIGINAL


FILE_NOT_FOUND: Literal['file not found'] = 'file not found'
INVALID_MODULE: Literal['invalid module'] = 'invalid module'
INVALID_SCRIPT: Literal['invalid script'] = 'invalid script'

MODULE_AI_ALGORITHM: Literal['ai_tool_original'] = 'ai_tool_original'
MODULE_ROBOTIC_SYSTEM: Literal['input_tool_original'] = 'input_tool_original'
MODULE_ANALYSIS: Literal['output_tool_original'] = 'output_tool_original'


@dataclass
class NimoParameters:
    num_objectives: int
    num_cycles: int

    candidates_file: Path
    proposals_file: Path
    
    input_folder: Path
    output_folder: Path

    custom_ai_algorithm: Optional[Path]
    custom_robotic_system: Optional[Path]
    custom_analysis: Optional[Path]

    program: list[tuple[str, int, str]] = field(default_factory=list)

    ai_algorithm_options: dict[str, dict[str, Any]] = field(default_factory=dict)

    def get_custom_ai_algorithm_module(self) -> Optional[ModuleType]:
        """Returns the module of the customized AI algorithm."""
        assert self.custom_ai_algorithm is not None
        module: Optional[ModuleType] = NimoParameters._load_module(self.custom_ai_algorithm, 'ai_tool_original')
        return module

    def get_custom_robotic_system_module(self) -> Optional[ModuleType]:
        """Returns the module of the customized robotic system."""
        assert self.custom_robotic_system is not None
        module: Optional[ModuleType] = NimoParameters._load_module(self.custom_robotic_system, 'input_tool_original')
        return module

    def get_custom_analysis_module(self) -> Optional[ModuleType]:
        """Returns the module of the customized analysis."""
        if self.custom_analysis is None:
            self.custom_analysis = self.input_folder.joinpath('output_tool_original.py')
        module: Optional[ModuleType] = NimoParameters._load_module(self.custom_analysis, 'output_tool_original')
        return module

    def check(self) -> Optional[str]:
        """Checks whether the parameters are valid or not.

        Returns:
            Optional[str]: An error message if any parameters are invalid; otherwise, None.
        """
        includes_custom_ai_algorithm: bool = False
        includes_custom_robotic_system: bool = False

        error: Optional[str] = NimoParameters._check_csv(self.candidates_file)
        if error:
            return error 

        for cycle_setting in self.program:
            ai_algorithm, _, robotic_system = cycle_setting
            if ai_algorithm == AI_ALGORITHM_ORIGINAL:
                includes_custom_ai_algorithm = True
            if robotic_system == ROBOTIC_SYSTEM_ORIGINAL:
                includes_custom_robotic_system = True

        if includes_custom_ai_algorithm:
            error = self._check_script_file(self.custom_ai_algorithm, MODULE_AI_ALGORITHM)
            if error:
                return error

        if includes_custom_robotic_system:
            error = self._check_script_file(self.custom_robotic_system, MODULE_ROBOTIC_SYSTEM)
            if error:
                return error
            error = self._check_script_file(self.custom_analysis, MODULE_ANALYSIS)
            if error:
                return error
        
        return None
    
    def is_pdc_used(self) -> bool:
        """Checks whether the PDC algorithm is used in the program or not.

        Returns:
            bool: True if the PDC algorithm is used in the program; otherwise, False.
        """
        for (ai_algorithm, _, _) in self.program:
            if ai_algorithm == AI_ALGORITHM_PDC:
                return True
        return False
    
    @staticmethod
    def _check_csv(file: Path) -> Optional[str]:
        """Checks whether the file is a valid CSV file.

        Args:
            file (Path): The path to the file.

        Returns:
            Optional[str]: An error message if the file is invalid; otherwise, None.
        """
        if file is None or not file.is_file():
            return f'Input file ({file}) does not exist.'
        import pandas as pd
        try:
            df: pd.DataFrame = pd.read_csv(file, encoding = 'utf8')
        except:
            return f'"Input file ({file.name}) is not a valid CSV file.'

    @staticmethod
    def _get_label(module_name: str) -> str:
        return {
            MODULE_AI_ALGORITHM: 'AI algorithm script',
            MODULE_ROBOTIC_SYSTEM: 'robotic system script',
            MODULE_ANALYSIS: 'analysis script',
        }[module_name]
    
    @staticmethod
    def _get_method_name(module_name: str) -> str:
        return {
            MODULE_AI_ALGORITHM: 'select',
            MODULE_ROBOTIC_SYSTEM: 'perform',
            MODULE_ANALYSIS: 'perform',
        }[module_name]
    
    def _check_script_file(self, path: Optional[Path], module_name: str) -> Optional[str]:
        """Checks whether the original script file is valid or not.

        Args:
            path (Optional[Path]): The path to the script file to check.
            module_name (str): The name of the module.

        Returns:
            Optional[str]: An error message if the script file is invalid; otherwise, None.
        """
        _, error = self._load_custom_script(path, module_name)
        if error is None:
            return None
        label: str = NimoParameters._get_label(module_name)
        if error == FILE_NOT_FOUND:
            if path is None:
                return f'Default {label} ({module_name}.py) is not found. \nCheck your input folder ({self.input_folder}).'
            else:
                return f'Selected {label} ({path.name}) is not found.'
        if error == INVALID_MODULE:
            if path is None:
                return f'Default {label} ({module_name}.py) is invalid python script. \nCheck your input folder ({self.input_folder}).'
            else:
                return f'Selected {label} ({path.name}) is invalid python script.'
        if error == INVALID_SCRIPT:
            method: str = NimoParameters._get_method_name(module_name)
            if path is None:
                return f'Default {label} ({module_name}.py) is invalid.\nCheck that the “ORIGINAL” class, including a “{method}” instance method, is defined in the script file.'
            else:
                return f'Selected {label} ({path.name}) is invalid.\nCheck that the “ORIGINAL” class, including a “{method}” instance method, is defined in the script file.'
        return f'Unknown error occurred during the check of {label} script file.'
            
    def _load_custom_script(
            self,
            script_path: Optional[Path],
            module_name: str) -> tuple[Optional[ModuleType], Optional[str]]:
        """Loads the Python module from the script file.

        Args:
            script_path (Optional[Path]): The path to the script file of the target module.
            module_name (str): The name of the module to load.

        Returns:
            tuple[Optional[ModuleType], Optional[str]]: A tuple containing the module type and an error message.
        """
        if script_path is None:
            script_path = self.input_folder.joinpath(f'{module_name}.py')
        if not script_path.is_file():
            return None, FILE_NOT_FOUND

        try:
            module: ModuleType = NimoParameters._load_module(script_path, module_name)
        except:
            return None, INVALID_MODULE
        
        if not NimoParameters._check_custom_script(module):
            return None, INVALID_SCRIPT
        
        return module, None
    
    @staticmethod
    def _check_custom_script(module: ModuleType):
        """_summary_

        Args:
            module (ModuleType): _description_

        Returns:
            _type_: _description_
        """
        if not hasattr(module, 'ORIGINAL'):
            return False
        else:
            original = getattr(module, 'ORIGINAL')
            if not hasattr(original, 'perform'):
                return False
        return True

    @staticmethod
    def _load_module(path: Path, module_name: str) -> ModuleType:
        """Loads Python module from the script file.

        Args:
            path (Path): The path to the script file of the target module.
            module_name (str): The name of the module to load.

        Returns:
            Any: Python module.
        """
        from importlib._bootstrap_external import spec_from_file_location
        from importlib._bootstrap import module_from_spec

        spec = spec_from_file_location(module_name, path)
        assert spec is not None
        loader = spec.loader
        assert loader is not None
        module = module_from_spec(spec)
        loader.exec_module(module)
        return module
    