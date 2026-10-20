from typing import Literal
from api.sema.base import SemaType
from api.sema.property_format import UTCMilliseconds
from api.sema.property_format import UUID4Str


class GridworksEventProblem(SemaType):
    """Sema: https://schemas.electricity.works/types/gridworks.event.problem/001"""

    src: str
    problem_type: str
    summary: str
    details: str
    time_created_ms: UTCMilliseconds
    message_id: UUID4Str
    type_name: Literal["gridworks.event.problem"] = "gridworks.event.problem"
    version: Literal["001"] = "001"
