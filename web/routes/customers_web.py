from typing import Annotated
from fastapi import APIRouter, Request, Depends, Form, HTTPException
from backend.core.catalogs import CatalogCode
from backend.services.customer_service import CustomerService
from ..templating import templates
from ..deps import WebAdminOnly, WebCatalogDB, get_web_catalog_code
from ..context import build_nav_context

router = APIRouter(prefix="/admin/customers", tags=["web-admin-customers"])


@router.get("")
async def admin_customers_page(
    request: Request,
    current_user: WebAdminOnly,
    catalog_db: WebCatalogDB,
    catalog_code: Annotated[CatalogCode, Depends(get_web_catalog_code)],
):
    service = CustomerService(catalog_db)
    customers = await service.list_customers()

    context = await build_nav_context(current_user, catalog_code, catalog_db)
    context.update({"active_nav": "admin_customers", "customers": customers})
    return templates.TemplateResponse(request, "admin_customers.html", context)


@router.post("")
async def create_customer(
    request: Request,
    current_user: WebAdminOnly,
    catalog_db: WebCatalogDB,
    catalog_code: Annotated[CatalogCode, Depends(get_web_catalog_code)],
    customer_id: str = Form(...),
    customer_name: str = Form(...),
):
    service = CustomerService(catalog_db)
    error_message = None
    success_message = None
    try:
        await service.create_customer(customer_id, customer_name, current_user.id)
        success_message = f"Customer '{customer_id}' created."
    except ValueError as e:
        error_message = str(e)

    customers = await service.list_customers()
    context = await build_nav_context(current_user, catalog_code, catalog_db)
    context.update({
        "active_nav": "admin_customers", "customers": customers,
        "error_message": error_message, "success_message": success_message,
    })
    return templates.TemplateResponse(request, "admin_customers.html", context)


@router.post("/{customer_id}/edit")
async def edit_customer(
    request: Request,
    customer_id: str,
    current_user: WebAdminOnly,
    catalog_db: WebCatalogDB,
    catalog_code: Annotated[CatalogCode, Depends(get_web_catalog_code)],
    new_customer_id: str = Form(...),
    customer_name: str = Form(...),
):
    service = CustomerService(catalog_db)
    error_message = None
    success_message = None
    try:
        await service.update_customer(customer_id, new_customer_id, customer_name, current_user.id)
        success_message = f"Customer '{customer_id}' updated."
    except ValueError as e:
        error_message = str(e)

    customers = await service.list_customers()
    context = await build_nav_context(current_user, catalog_code, catalog_db)
    context.update({
        "active_nav": "admin_customers", "customers": customers,
        "error_message": error_message, "success_message": success_message,
    })
    return templates.TemplateResponse(request, "admin_customers.html", context)


@router.post("/{customer_id}/delete")
async def delete_customer(
    request: Request,
    customer_id: str,
    current_user: WebAdminOnly,
    catalog_db: WebCatalogDB,
    catalog_code: Annotated[CatalogCode, Depends(get_web_catalog_code)],
):
    service = CustomerService(catalog_db)
    error_message = None
    success_message = None
    try:
        await service.delete_customer(customer_id, current_user.id)
        success_message = f"Customer '{customer_id}' removed."
    except ValueError as e:
        error_message = str(e)

    customers = await service.list_customers()
    context = await build_nav_context(current_user, catalog_code, catalog_db)
    context.update({
        "active_nav": "admin_customers", "customers": customers,
        "error_message": error_message, "success_message": success_message,
    })
    return templates.TemplateResponse(request, "admin_customers.html", context)


async def _render_detail(
    request: Request,
    customer_id: str,
    current_user,
    catalog_db,
    catalog_code: CatalogCode,
    service: CustomerService,
    error_message: str | None = None,
    success_message: str | None = None,
):
    customer = await service.validate_customer(customer_id)
    if not customer:
        raise HTTPException(status_code=404, detail=f"Customer {customer_id} not found")

    context = await build_nav_context(current_user, catalog_code, catalog_db)
    context.update({
        "active_nav": "admin_customers", "customer": customer,
        "error_message": error_message, "success_message": success_message,
    })
    return templates.TemplateResponse(request, "admin_customer_detail.html", context)


@router.get("/{customer_id}")
async def admin_customer_detail_page(
    request: Request,
    customer_id: str,
    current_user: WebAdminOnly,
    catalog_db: WebCatalogDB,
    catalog_code: Annotated[CatalogCode, Depends(get_web_catalog_code)],
):
    service = CustomerService(catalog_db)
    return await _render_detail(request, customer_id, current_user, catalog_db, catalog_code, service)


@router.post("/{customer_id}/locations")
async def create_location(
    request: Request,
    customer_id: str,
    current_user: WebAdminOnly,
    catalog_db: WebCatalogDB,
    catalog_code: Annotated[CatalogCode, Depends(get_web_catalog_code)],
    location_id: str = Form(...),
    location_name: str = Form(...),
):
    service = CustomerService(catalog_db)
    error_message = None
    success_message = None
    try:
        await service.create_location(customer_id, location_id, location_name, current_user.id)
        success_message = f"Location '{location_id}' created."
    except ValueError as e:
        error_message = str(e)

    return await _render_detail(
        request, customer_id, current_user, catalog_db, catalog_code, service, error_message, success_message
    )


@router.post("/{customer_id}/locations/{location_id}/edit")
async def edit_location(
    request: Request,
    customer_id: str,
    location_id: str,
    current_user: WebAdminOnly,
    catalog_db: WebCatalogDB,
    catalog_code: Annotated[CatalogCode, Depends(get_web_catalog_code)],
    new_location_id: str = Form(...),
    location_name: str = Form(...),
):
    service = CustomerService(catalog_db)
    error_message = None
    success_message = None
    try:
        await service.update_location(customer_id, location_id, new_location_id, location_name, current_user.id)
        success_message = f"Location '{location_id}' updated."
    except ValueError as e:
        error_message = str(e)

    return await _render_detail(
        request, customer_id, current_user, catalog_db, catalog_code, service, error_message, success_message
    )


@router.post("/{customer_id}/locations/{location_id}/delete")
async def delete_location(
    request: Request,
    customer_id: str,
    location_id: str,
    current_user: WebAdminOnly,
    catalog_db: WebCatalogDB,
    catalog_code: Annotated[CatalogCode, Depends(get_web_catalog_code)],
):
    service = CustomerService(catalog_db)
    error_message = None
    success_message = None
    try:
        await service.delete_location(customer_id, location_id, current_user.id)
        success_message = f"Location '{location_id}' removed."
    except ValueError as e:
        error_message = str(e)

    return await _render_detail(
        request, customer_id, current_user, catalog_db, catalog_code, service, error_message, success_message
    )
