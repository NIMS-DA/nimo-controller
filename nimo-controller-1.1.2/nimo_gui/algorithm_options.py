"""Data classes for AI algorithm options"""

from dataclasses import dataclass, field
from typing import Any, Literal, Optional

from nimo_gui.constants import (DEFAULT_OPTION_BLOX_OUTPUT_RES, DEFAULT_OPTION_PDC_ESTIMATION,
                                DEFAULT_OPTION_PDC_OUTPUT_RES, DEFAULT_OPTION_PDC_SAMPLING,
                                DEFAULT_OPTION_PHYSBO_ARD_MULTI, DEFAULT_OPTION_PHYSBO_ARD_SINGLE,
                                DEFAULT_OPTION_PHYSBO_OUTPUT_RES_MULTI, DEFAULT_OPTION_PHYSBO_OUTPUT_RES_SINGLE,
                                DEFAULT_OPTION_PHYSBO_SCORE_MULTI, DEFAULT_OPTION_PHYSBO_SCORE_SINGLE,
                                DEFAULT_OPTION_PTR_OUTPUT_RES, DEFAULT_OPTION_SLESA_BETA_MAX,
                                DEFAULT_OPTION_SLESA_BETA_NUM, DEFAULT_OPTION_SLESA_OUTPUT_RES)


@dataclass
class ReOptions:
    re_seed: Optional[int] = None


@dataclass
class PhysboOptions:
    physbo_score: str
    ard: bool
    output_res: bool

    @staticmethod
    def single() -> 'PhysboOptions':
        return PhysboOptions(
            physbo_score=DEFAULT_OPTION_PHYSBO_SCORE_SINGLE,
            ard=DEFAULT_OPTION_PHYSBO_ARD_SINGLE,
            output_res=DEFAULT_OPTION_PHYSBO_OUTPUT_RES_SINGLE
        )

    @staticmethod
    def multi() -> 'PhysboOptions':
        return PhysboOptions(
            physbo_score=DEFAULT_OPTION_PHYSBO_SCORE_MULTI,
            ard=DEFAULT_OPTION_PHYSBO_ARD_MULTI,
            output_res=DEFAULT_OPTION_PHYSBO_OUTPUT_RES_MULTI
        )


@dataclass
class BloxOptions:
    output_res: bool = DEFAULT_OPTION_BLOX_OUTPUT_RES


@dataclass
class PdcOptions:
    pdc_estimation: str = DEFAULT_OPTION_PDC_ESTIMATION
    pdc_sampling: str = DEFAULT_OPTION_PDC_SAMPLING
    output_res: bool = DEFAULT_OPTION_PDC_OUTPUT_RES


@dataclass
class SlesaOptions:
    slesa_beta_max: float = DEFAULT_OPTION_SLESA_BETA_MAX
    slesa_beta_num: int = DEFAULT_OPTION_SLESA_BETA_NUM
    re_seed: Optional[int] = None
    output_res: bool = DEFAULT_OPTION_SLESA_OUTPUT_RES


@dataclass
class PtrOptions:
    ptr_ranges: list[list[float | Literal['min', 'max']]] = field(default_factory=list)
    output_res: bool = DEFAULT_OPTION_PTR_OUTPUT_RES

    def __post_init__(self):
        if len(self.ptr_ranges) == 0:
            self.ptr_ranges.append(['min', 'max'])
    
    def to_dict(self, num_objectives: int) -> dict[str, Any]:
        return {
            'ptr_ranges': self.ptr_ranges[:num_objectives],
            'output_res': self.output_res
        }


@dataclass
class AiAlgorithmOptions:
    re: ReOptions = field(default_factory=ReOptions)
    physbo_single: PhysboOptions = field(default_factory=PhysboOptions.single)
    physbo_multi: PhysboOptions = field(default_factory=PhysboOptions.multi)
    blox: BloxOptions = field(default_factory=BloxOptions)
    pdc: PdcOptions = field(default_factory=PdcOptions)
    slesa: SlesaOptions = field(default_factory=SlesaOptions)
    ptr: PtrOptions = field(default_factory=PtrOptions)
