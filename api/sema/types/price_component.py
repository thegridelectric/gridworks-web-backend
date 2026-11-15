from typing import Literal
from pydantic import StrictInt
from api.sema.base import SemaType
from api.sema.enums import PriceComponentName


class PriceComponent(SemaType):
    """Sema: https://schemas.electricity.works/types/price.component/000"""

    name: PriceComponentName
    price_x1000: StrictInt
    type_name: Literal["price.component"] = "price.component"
    version: Literal["000"] = "000"
