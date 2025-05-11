from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field, RootModel


class PortsModel(BaseModel):
    """
    Model for Ports
    """

    id: Optional[str] = Field(default=None)  # ID field that can be added in code
    product: str
    section: str
    from_port: str = Field(alias="fromPort")
    to_port: str = Field(alias="toPort")
    protocol: str
    port: str
    description: str

    class Config:
        populate_by_name = True  # Allow populating by field name and alias


class ServiceModel(BaseModel):
    """
    Model for Services returned by the root endpoint
    """

    id: str
    name: str


class SourceModel(BaseModel):
    """
    Model for Source services returned by the source endpoint
    """

    id: str
    product: str
    from_port: str = Field(alias="fromPort")
    section: str

    class Config:
        populate_by_name = True


# Request models
class ProductRequest(BaseModel):
    """Model for validating product requests"""

    product_name: str = Field(..., alias="productName")

    class Config:
        populate_by_name = True


class SourceTargetRequest(BaseModel):
    """Model for validating source/target requests"""

    from_port: str = Field(..., alias="fromPort")
    product_name: str = Field(..., alias="productName")

    class Config:
        populate_by_name = True


class SourceTargetSectionRequest(BaseModel):
    """Model for validating source/target section requests"""

    from_port: str = Field(..., alias="fromPort")
    to_port: str = Field(..., alias="toPort")
    product_name: str = Field(..., alias="productName")
    section: str

    class Config:
        populate_by_name = True


class PortsRequest(BaseModel):
    """Model for validating ports requests"""

    from_port: str = Field(..., alias="fromPort")
    to_port: str = Field(..., alias="toPort")
    product_name: str = Field(..., alias="productName")

    class Config:
        populate_by_name = True


class ExcelDataRequest(RootModel):
    """Model for validating Excel data requests"""

    # Use RootModel to handle a list of dictionaries
    root: List[Dict[str, Any]]

    # No need for Config with RootModel


class ErrorResponse(BaseModel):
    """Model for error responses"""

    error: str
    details: Optional[List[str]] = None

    class Config:
        populate_by_name = True


class FileUrlResponse(BaseModel):
    """Model for file URL responses"""

    file_url: str = Field(..., alias="fileUrl")

    class Config:
        populate_by_name = True
