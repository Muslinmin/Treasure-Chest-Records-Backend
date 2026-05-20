from dotenv import load_dotenv

import os

from fastapi import Security, HTTPException, status
from fastapi.security import APIKeyHeader

load_dotenv()

API_KEY_NAME="API-KEY"
FAST_API_KEY = os.getenv("FAST_API_KEY")

api_key_header = APIKeyHeader(name=API_KEY_NAME, auto_error=True)



async def verify_api_key(api_key: str = Security(api_key_header)):
    if api_key != FAST_API_KEY:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
        )
    return api_key
