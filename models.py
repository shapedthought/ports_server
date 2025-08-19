from pydantic import BaseModel, Field


class PortResponse(BaseModel):
    description: str = Field(..., alias="description")
    product: str = Field(..., alias="product")
    sourceService: str = Field(..., alias="sourceService")
    targetService: str = Field(..., alias="targetService")
    subheading: str = Field(..., alias="subheading")
    subheadingL2: str = Field(..., alias="subheadingL2")
    subheadingL3: str = Field(..., alias="subheadingL3")
    port: str = Field(..., alias="port")
    protocol: str = Field(..., alias="protocol")


class TargetRequest(BaseModel):
    sourceService: str
    productName: str
    subheading: str


class PortRequest(BaseModel):
    productName: str
    sourceService: str
    targetService: str


class SourceRequest(BaseModel):
    productName: str


class SourceResponse(BaseModel):
    product: str
    subheading: str
    sourceService: str
