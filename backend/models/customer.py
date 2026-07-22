from sqlalchemy import String, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship
from ..core.database import CatalogBase


class Customer(CatalogBase):
    __tablename__ = "customers"

    customer_id: Mapped[str] = mapped_column(String(50), primary_key=True, index=True)
    customer_name: Mapped[str] = mapped_column(String(255), nullable=False)

    locations: Mapped[list["Location"]] = relationship("Location", back_populates="customer", cascade="all, delete-orphan")
    transactions: Mapped[list["Transaction"]] = relationship("Transaction", back_populates="customer")


class Location(CatalogBase):
    __tablename__ = "locations"

    location_id: Mapped[str] = mapped_column(String(50), primary_key=True, index=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.customer_id"), nullable=False, index=True)
    location_name: Mapped[str] = mapped_column(String(255), nullable=False)

    customer: Mapped["Customer"] = relationship("Customer", back_populates="locations")
    transactions: Mapped[list["Transaction"]] = relationship("Transaction", back_populates="location")
