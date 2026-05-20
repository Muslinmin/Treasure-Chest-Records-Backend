from dotenv import load_dotenv

import os

from fastapi import Security, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

import hmac

load_dotenv()

FAST_API_KEY = os.getenv("FAST_API_KEY")
if not FAST_API_KEY:
    raise RuntimeError("FAST_API_KEY is not set")


api_key_header = HTTPBearer(auto_error=True)



async def verify_api_key(api_key: HTTPAuthorizationCredentials = Security(api_key_header)):
    if not hmac.compare_digest(api_key.credentials, FAST_API_KEY):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
        )
    return api_key
