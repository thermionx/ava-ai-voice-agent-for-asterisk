"""Authenticated household caller management; memory remains the source of truth."""
import os
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from auth import get_current_user

router = APIRouter(prefix="/trusted-callers", dependencies=[Depends(get_current_user)])


class TrustedCaller(BaseModel):
    create_only: bool = True
    caller_number: str = Field(min_length=7, max_length=40)
    caller_name: str = Field(default="", max_length=120)
    business_name: str = Field(default="", max_length=120)


async def memory_request(method, *, data=None, phone=None):
    base = os.environ.get("OPERATOR_ZERO_MEMORY_URL", "http://127.0.0.1:8790").rstrip("/")
    token = os.environ.get("OPERATOR_ZERO_MEMORY_ADMIN_TOKEN", "")
    if not token:
        raise HTTPException(503, "Trusted caller management is not configured on this server.")
    try:
        async with httpx.AsyncClient(timeout=5.0, trust_env=False, follow_redirects=False) as client:
            reply = await client.request(
                method, base + "/admin/trusted-callers", json=data,
                params={"phone": phone} if phone is not None else None,
                headers={"Authorization": "Bearer " + token},
            )
        if reply.status_code == 400:
            raise HTTPException(400, reply.json().get("error", "Invalid caller details"))
        reply.raise_for_status()
        result = reply.json()
        if not isinstance(result, dict):
            raise ValueError("Invalid memory response")
        if method == "GET" and (not isinstance(result.get("callers"), list) or not isinstance(result.get("count"), int)):
            raise ValueError("Invalid directory response")
        return result
    except (httpx.HTTPError, ValueError):
        # Never return upstream URLs, credentials, or exception text to the client.
        raise HTTPException(503, "Household memory is unavailable. Your saved list has not been cleared.")


@router.get("")
async def list_callers(response: Response):
    response.headers["Cache-Control"] = "no-store"
    return await memory_request("GET")


@router.post("")
async def save_caller(caller: TrustedCaller, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return await memory_request("POST", data=caller.model_dump())


@router.delete("")
async def require_screening(phone: str, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return await memory_request("DELETE", phone=phone)


@router.get("/phonebook")
async def phonebook_settings(response: Response):
    response.headers["Cache-Control"] = "no-store"
    url = os.environ.get("OPERATOR_ZERO_PHONEBOOK_URL", "").strip()
    parsed = urlsplit(url)
    valid = parsed.scheme in {"http", "https"} and bool(parsed.hostname) and not parsed.username and not parsed.password
    return {
        "enabled": bool(valid and os.environ.get("OPERATOR_ZERO_PHONEBOOK_PASSWORD")),
        "url": url if valid else "",
        "username": "phonebook",
    }


@router.get("/phonebook/password")
async def phonebook_password(response: Response):
    response.headers["Cache-Control"] = "no-store"
    value = os.environ.get("OPERATOR_ZERO_PHONEBOOK_PASSWORD", "")
    if not value:
        raise HTTPException(503, "Phonebook access is not configured.")
    return {"password": value}
