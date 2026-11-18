from fastapi import APIRouter

from app.routers import core, remount

api = APIRouter()
api.include_router(core.router)
api.include_router(remount.router)
