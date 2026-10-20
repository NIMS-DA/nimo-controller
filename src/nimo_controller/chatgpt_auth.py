"""ChatGPT sign-in, credential storage, and streaming inference.

Credentials use a shared encrypted format backed by the system keyring.
A file lock serializes rotating refresh tokens
across controller processes sharing the same configuration directory.
"""
from __future__ import annotations

import asyncio
import base64
import ctypes
import hashlib
import json
import os
import secrets
import ssl
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import certifi
import jwt
from filelock import AsyncFileLock
from pydantic_ai.models.openai import OpenAIResponsesModel
import keyring
from cryptography.fernet import Fernet
import tempfile

ISSUER = "https://auth.openai.com"
AUTHORIZE = ISSUER + "/api/accounts/authorize"
TOKEN = ISSUER + "/api/accounts/oauth/token"
RESOURCE = "https://api.openai.com/v1"
PLAN_SCOPE = "chatgpt.tokens.use.direct"
SCOPES = "openid profile email offline_access resource.invoke " + PLAN_SCOPE
USAGE_URL = "https://chatgpt.com/settings/usage"


MAGIC = b"NIMO-AUTH-1\n"
SERVICE = "nimo-controller.chatgpt"
TRUSTED_BACKENDS = {
    "keyring.backends.Windows", "keyring.backends.macOS",
    "keyring.backends.SecretService", "keyring.backends.kwallet",
}


class CredentialStoreError(RuntimeError):
    pass


def _keyring_backend():
    backend = keyring.get_keyring()
    candidates = (backend.backends if type(backend).__module__ == "keyring.backends.chainer"
                  else [backend])
    for candidate in candidates:
        if type(candidate).__module__ in TRUSTED_BACKENDS:
            return candidate
    raise CredentialStoreError(
        "A secure system keyring is required. Enable and unlock your system "
        "credential store, then retry. Plaintext storage is disabled."
    )


class CredentialStore:
    def __init__(self, path: Path):
        self.path = path

    def _cipher(self, key_id: str, *, create: bool = False) -> Fernet:
        try:
            backend = _keyring_backend()
            key = backend.get_password(SERVICE, key_id)
            if key is None and create:
                key = Fernet.generate_key().decode("ascii")
                backend.set_password(SERVICE, key_id, key)
                if backend.get_password(SERVICE, key_id) != key:
                    raise ValueError("Key storage verification failed")
            if key is None:
                raise CredentialStoreError(
                    "The ChatGPT encryption key is missing from the system keyring. "
                    "Restore access to the original keyring; the credential file has not been changed."
                )
            return Fernet(key.encode("ascii"))
        except CredentialStoreError:
            raise
        except Exception:
            raise CredentialStoreError(
                "Could not access the secure system keyring. Unlock it and retry; "
                "the credential file has not been changed."
            ) from None

    @staticmethod
    def _parts(payload: bytes):
        key_id, ciphertext = payload[len(MAGIC):].split(b"\n", 1)
        key_id = str(uuid.UUID(key_id.decode("ascii")))
        return key_id, ciphertext

    def read(self, legacy_decode) -> dict:
        try:
            payload = self.path.read_bytes()
            if payload.startswith(MAGIC):
                key_id, ciphertext = self._parts(payload)
                data = json.loads(self._cipher(key_id).decrypt(ciphertext))
            else:
                data = json.loads(legacy_decode(payload))
            if not isinstance(data, dict) or not isinstance(data.get("profiles"), dict) or not data.get("host_id"):
                raise ValueError("Invalid credential structure")
            if not payload.startswith(MAGIC):
                self.write(data)  # Migrate only after successful legacy decryption.
            return data
        except CredentialStoreError:
            raise
        except Exception:
            raise CredentialStoreError(
                "Could not decrypt ChatGPT credentials. The file has not been changed."
            ) from None

    def write(self, data: dict) -> None:
        try:
            existing = self.path.read_bytes() if self.path.exists() else b""
            modern = existing.startswith(MAGIC)
            key_id = self._parts(existing)[0] if modern else str(uuid.uuid4())
            cipher = self._cipher(key_id, create=not modern)
            plain = json.dumps(data).encode("utf-8")
            ciphertext = cipher.encrypt(plain)
            if cipher.decrypt(ciphertext) != plain:
                raise ValueError("Encryption verification failed")
            payload = MAGIC + key_id.encode("ascii") + b"\n" + ciphertext
            fd, temporary = tempfile.mkstemp(dir=self.path.parent, prefix=".chatgpt-")
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        except CredentialStoreError:
            raise
        except Exception:
            raise CredentialStoreError(
                "Could not save encrypted ChatGPT credentials. Check storage permissions and retry."
            ) from None


class ChatGPTError(RuntimeError):
    """A credential-free error suitable for display."""
    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        self.code = code


def usage_error_code(error) -> str | None:
    """Recognize both streamed errors and HTTP errors wrapped by the SDK."""
    code = getattr(error, "code", None)
    body = error if isinstance(error, dict) else getattr(error, "body", None)
    if isinstance(body, dict):
        detail = body.get("error", body)
        if isinstance(detail, dict):
            code = detail.get("code", code)
    return code if code in (
        "subscription_sharing_usage_limit_exceeded", "subscription_sharing_usage_unavailable"
    ) else None


def _protect(data: bytes, *, decrypt: bool = False) -> bytes:
    """Legacy decoder for migration only; new writes use CredentialStore."""
    if os.name != "nt":
        return data
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
                         ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
        raise ChatGPTError("Could not access protected ChatGPT credentials.")
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        kernel.LocalFree(output.data)


class ChatGPTAuth:
    def __init__(self, directory: Path):
        self.directory = directory
        self.path = directory / "chatgpt-auth.bin"
        self.store = CredentialStore(self.path)
        self.lock = AsyncFileLock(str(directory / "chatgpt-auth.lock"), timeout=30)
        self.local_lock = asyncio.Lock()
        self.pending: dict | None = None
        self.listener: asyncio.Server | None = None
        self.timer: asyncio.Task | None = None
        self.exchange_task: asyncio.Task | None = None
        self.result: dict = {"status": "idle"}

    @asynccontextmanager
    async def _locked(self):
        # FileLock versions can be reentrant within a process; serialize tasks too.
        async with self.local_lock:
            async with self.lock:
                yield

    def _read(self) -> dict:
        if not self.path.exists():
            return {"host_id": "urn:uuid:" + str(uuid.uuid4()), "profiles": {}, "active": None}
        try:
            return self.store.read(lambda payload: _protect(payload, decrypt=True))
        except CredentialStoreError as exc:
            raise ChatGPTError(str(exc)) from None

    def _write(self, data: dict) -> None:
        try:
            self.store.write(data)
        except CredentialStoreError as exc:
            raise ChatGPTError(str(exc)) from None

    async def status(self) -> dict:
        async with self._locked():
            data = self._read()
        profiles = [
            {"id": key, "label": f"Connection {index + 1}",
             "email": value.get("email", ""), "connected": bool(value.get("access_token")),
             "sharing": bool(value.get("access_token")) and PLAN_SCOPE in value.get("scopes", []),
             "welcome_seen": [value.get("issuer"), value.get("subject")] in data.get("welcomed_accounts", [])}
            for index, (key, value) in enumerate(data["profiles"].items())
        ]
        return {"profiles": profiles, "active": data["active"], "attempt": self.result,
                "usage_url": USAGE_URL}

    async def acknowledge_welcome(self, profile_id: str) -> None:
        async with self._locked():
            data = self._read()
            profile = data["profiles"].get(profile_id, {})
            if not profile.get("subject") or PLAN_SCOPE not in profile.get("scopes", []):
                raise ChatGPTError("Connect a ChatGPT account with plan usage enabled first.")
            identity = [profile["issuer"], profile["subject"]]
            welcomed = data.setdefault("welcomed_accounts", [])
            if identity not in welcomed:
                welcomed.append(identity)
            self._write(data)

    async def select(self, profile_id: str) -> None:
        async with self._locked():
            data = self._read()
            if profile_id not in data["profiles"]:
                raise ChatGPTError("Choose a saved ChatGPT account.")
            data["active"] = profile_id
            self._write(data)

    async def start(self, profile_id: str | None = None) -> str:
        await self.cancel()
        async with self._locked():
            data = self._read()
            if profile_id and profile_id not in data["profiles"]:
                raise ChatGPTError("Unknown ChatGPT account.")
            self._write(data)  # Persist the installation ID before registration.
            profile = data["profiles"].get(profile_id, {})
        verifier = secrets.token_urlsafe(48)
        self.pending = {
            "state": secrets.token_urlsafe(32), "nonce": secrets.token_urlsafe(32),
            "verifier": verifier, "profile_id": profile_id,
            "client_id": profile.get("client_id"), "expires": time.time() + 600,
        }
        self.listener = await asyncio.start_server(self._callback, "127.0.0.1", 0, limit=16384)
        port = self.listener.sockets[0].getsockname()[1]
        self.pending["redirect_uri"] = f"http://127.0.0.1:{port}/auth/callback"
        params = {
            "client_id": profile.get("client_id") or "dynamic_agent_client",
            "ext_agent_host_id": data["host_id"], "response_type": "code",
            "redirect_uri": self.pending["redirect_uri"], "scope": SCOPES,
            "resource": RESOURCE, "state": self.pending["state"], "nonce": self.pending["nonce"],
            "code_challenge_method": "S256",
            "code_challenge": base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("="),
        }
        if profile.get("client_id"):
            if profile.get("id_token"):
                params["id_token_hint"] = profile["id_token"]
            if profile.get("email"):
                params["login_hint"] = profile["email"]
        else:
            params["agent_name_hint"] = "NIMO Controller"
        self.result = {"status": "pending"}
        self.timer = asyncio.create_task(self._expire())
        return AUTHORIZE + "?" + urlencode(params)

    async def _expire(self) -> None:
        await asyncio.sleep(600)
        await self.cancel()
        self.result = {"status": "error", "error": "Sign-in timed out. Try again."}

    async def cancel(self) -> None:
        if self.exchange_task and self.exchange_task is not asyncio.current_task():
            await asyncio.shield(self.exchange_task)
        self.pending = None
        if self.listener:
            self.listener.close()
            await self.listener.wait_closed()
            self.listener = None
        if self.timer and self.timer is not asyncio.current_task():
            self.timer.cancel()
        self.timer = None
        if self.result.get("status") == "pending":
            self.result = {"status": "idle"}

    async def _callback(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        message = "Sign-in could not be verified. Return to NIMO Controller and try again."
        status = "400 Bad Request"
        try:
            header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
            method, target, _ = header.split(b"\r\n", 1)[0].decode("ascii").split(" ")
            parsed = urlsplit(target)
            query = parse_qs(parsed.query)
            pending = self.pending
            if (method != "GET" or parsed.path != "/auth/callback" or not pending
                    or pending["expires"] < time.time()
                    or query.get("state") != [pending["state"]]
                    or any(len(values) != 1 for values in query.values())):
                raise ChatGPTError(message)
            self.pending = None  # Consume once, before any network await.
            self.exchange_task = asyncio.current_task()
            self.result = {"status": "exchanging"}
            try:
                profile_id = await self._finish(pending, {k: v[0] for k, v in query.items()})
                self.result = {"status": "complete", "profile_id": profile_id}
                message = "ChatGPT connected. Return to NIMO Controller. You can close this tab."
                status = "200 OK"
            except Exception as exc:
                message = str(exc) if isinstance(exc, ChatGPTError) else "ChatGPT sign-in failed. Try again."
                self.result = {"status": "error", "error": message}
            await self.cancel()
            self.exchange_task = None
        except Exception:
            pass
        finally:
            body = message.encode()
            writer.write((f"HTTP/1.1 {status}\r\nContent-Type: text/plain; charset=utf-8\r\n"
                          f"Content-Length: {len(body)}\r\nCache-Control: no-store\r\n"
                          "Referrer-Policy: no-referrer\r\nConnection: close\r\n\r\n").encode() + body)
            try:
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

    async def _token_request(self, values: dict) -> dict:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(TOKEN, data={**values, "resource": RESOURCE})
        if response.status_code != 200:
            raise ChatGPTError("ChatGPT authorization expired or failed. Sign in again.")
        data = response.json()
        if not data.get("access_token") or str(data.get("token_type", "")).lower() != "bearer":
            raise ChatGPTError("ChatGPT returned invalid credentials.")
        return data

    async def _identity(self, token: str, client_id: str, nonce: str) -> dict:
        def verify():
            # Match HTTPX's CA trust configuration instead of urllib's platform
            # default, which can contain stale certificates on Windows.
            if os.environ.get("SSL_CERT_FILE"):
                context = ssl.create_default_context(cafile=os.environ["SSL_CERT_FILE"])
            elif os.environ.get("SSL_CERT_DIR"):
                context = ssl.create_default_context(capath=os.environ["SSL_CERT_DIR"])
            else:
                context = ssl.create_default_context(cafile=certifi.where())
            keys = jwt.PyJWKClient(ISSUER + "/.well-known/jwks.json", timeout=15,
                                   ssl_context=context)
            key = keys.get_signing_key_from_jwt(token)
            claims = jwt.decode(token, key.key, algorithms=["RS256"], issuer=ISSUER,
                                audience=client_id,
                                options={"require": ["iss", "sub", "aud", "exp", "iat", "nonce"]})
            if not secrets.compare_digest(str(claims["nonce"]), nonce):
                raise ValueError("nonce")
            if not claims["sub"] or (isinstance(claims["aud"], list)
                                    and len(claims["aud"]) > 1
                                    and claims.get("azp") != client_id):
                raise ValueError("identity")
            return claims
        try:
            return await asyncio.to_thread(verify)
        except jwt.PyJWKClientConnectionError:
            raise ChatGPTError(
                "Could not securely retrieve ChatGPT signing keys. "
                "Check your network and CA certificates, then sign in again."
            ) from None
        except Exception:
            raise ChatGPTError("ChatGPT identity verification failed. Sign in again.") from None

    @staticmethod
    def _credentials(tokens: dict, previous: dict | None = None) -> dict:
        previous = previous or {}
        return {
            "access_token": tokens["access_token"],
            "refresh_token": tokens.get("refresh_token", previous.get("refresh_token")),
            "id_token": tokens.get("id_token", previous.get("id_token")),
            "scopes": tokens["scope"].split() if "scope" in tokens else previous.get("scopes", []),
            "expires_at": time.time() + float(tokens.get("expires_in", 0)),
        }

    async def _finish(self, pending: dict, query: dict) -> str:
        if query.get("error"):
            raise ChatGPTError("ChatGPT sign-in was declined. You can try again.")
        client_id = query.get("client_id") or pending["client_id"]
        if (not query.get("code") or not client_id or client_id == "dynamic_agent_client"
                or (pending["client_id"] and pending["client_id"] != client_id)):
            raise ChatGPTError("ChatGPT registration was incomplete or did not match.")
        profile_id = pending["profile_id"] or str(uuid.uuid4())
        async with self._locked():
            data = self._read()
            profile = data["profiles"].get(profile_id, {})
            # Retain the registration even if code exchange or identity verification fails.
            profile["client_id"] = client_id
            data["profiles"][profile_id] = profile
            self._write(data)
            tokens = await self._token_request({
                "grant_type": "authorization_code", "client_id": client_id,
                "code": query["code"], "code_verifier": pending["verifier"],
                "redirect_uri": pending["redirect_uri"],
            })
            claims = await self._identity(tokens.get("id_token", ""), client_id, pending["nonce"])
            if profile.get("subject") and (profile["subject"] != claims["sub"] or profile["issuer"] != claims["iss"]):
                raise ChatGPTError("The returned ChatGPT account does not match this connection.")
            profile.update(self._credentials(tokens))
            profile.update(subject=claims["sub"], issuer=claims["iss"], email=claims.get("email", ""))
            self._write(data)
        # Activation is a separate UI action, serialized against agent turns.
        return profile_id

    async def access_token(self, profile_id: str) -> str:
        async with self._locked():
            data = self._read()
            profile = data["profiles"].get(profile_id, {})
            if not profile.get("access_token"):
                raise ChatGPTError("Continue with ChatGPT in Agent settings to connect your account.")
            if PLAN_SCOPE not in profile.get("scopes", []):
                raise ChatGPTError("ChatGPT plan usage is not enabled. Sign in and allow plan usage.")
            if profile.get("expires_at", 0) <= time.time() + 60:
                if not profile.get("refresh_token"):
                    raise ChatGPTError("Your ChatGPT session expired. Sign in again.")
                tokens = await self._token_request({
                    "grant_type": "refresh_token", "client_id": profile["client_id"],
                    "refresh_token": profile["refresh_token"],
                })
                profile.update(self._credentials(tokens, profile))
                self._write(data)
                if PLAN_SCOPE not in profile["scopes"]:
                    raise ChatGPTError("ChatGPT plan usage permission is no longer enabled.")
            return profile["access_token"]

    async def models(self, profile_id: str) -> list[dict]:
        token = await self.access_token(profile_id)
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(RESOURCE + "/models", headers={"Authorization": f"Bearer {token}"})
        if response.status_code != 200:
            try:
                code = usage_error_code(response.json())
            except ValueError:
                code = None
            if code:
                raise ChatGPTError("Review your plan or this app's limit in ChatGPT settings.", code=code)
            raise ChatGPTError("Could not load ChatGPT models. Check plan access or sign in again.")
        return [{"id": row["slug"], "name": row.get("display_name", row["slug"])}
                for row in response.json()["models"] if row.get("visibility") == "list"]

    async def logout(self, profile_id: str) -> str | None:
        await self.cancel()
        warning = None
        async with self._locked():
            data = self._read()
            profile = data["profiles"].get(profile_id)
            if not profile:
                raise ChatGPTError("Unknown ChatGPT account.")
            if profile.get("refresh_token"):
                try:
                    async with httpx.AsyncClient(timeout=15) as client:
                        discovery = await client.get(ISSUER + "/.well-known/openid-configuration")
                        discovery.raise_for_status()
                        endpoint = discovery.json()["revocation_endpoint"]
                        if urlsplit(endpoint).scheme != "https" or urlsplit(endpoint).netloc != "auth.openai.com":
                            raise ValueError("Invalid revocation endpoint")
                        for attempt in range(3):
                            try:
                                response = await client.post(endpoint, data={
                                    "token": profile["refresh_token"], "token_type_hint": "refresh_token",
                                    "client_id": profile["client_id"],
                                })
                                if response.status_code < 500:
                                    break
                            except httpx.TransportError:
                                if attempt == 2:
                                    raise
                            await asyncio.sleep(0.5 * 2 ** attempt)
                        if response.status_code != 200:
                            raise ValueError("Revocation failed")
                except Exception:
                    warning = "Signed out locally. Remote revocation was not confirmed; disconnect the app in ChatGPT Settings."
            for key in ("access_token", "refresh_token", "id_token", "expires_at", "scopes"):
                profile.pop(key, None)
            self._write(data)
        return warning


class CompletedStream:
    """Reject failed, incomplete and truncated streams before committing a turn."""
    def __init__(self, stream):
        self.stream = stream

    def __getattr__(self, name):
        return getattr(self.stream, name)

    async def __aenter__(self):
        await self.stream.__aenter__()
        return self

    async def __aexit__(self, *args):
        return await self.stream.__aexit__(*args)

    async def __aiter__(self):
        completed = False
        async for event in self.stream:
            if event.type in ("response.failed", "response.incomplete", "error"):
                error = getattr(getattr(event, "response", None), "error", None)
                code = getattr(error, "code", None) or getattr(event, "code", None)
                if code in ("subscription_sharing_usage_limit_exceeded", "subscription_sharing_usage_unavailable"):
                    raise ChatGPTError("Review your plan or this app's limit in ChatGPT settings.", code=code)
                raise ChatGPTError("ChatGPT could not complete the response. Try again.")
            if event.type == "response.completed":
                completed = True
            yield event
        if not completed:
            raise ChatGPTError("ChatGPT disconnected before completing the response. Try again.")


class ChatGPTResponsesModel(OpenAIResponsesModel):
    async def _responses_create(self, messages, stream, model_settings, model_request_parameters):
        if not stream:
            raise ChatGPTError("ChatGPT plan requests must use streaming.")
        settings = {**model_settings, "openai_store": False,
                    "openai_previous_response_id": None}
        # Summaries are opt-in; without them reasoning produces no visible deltas.
        if self.profile.get("openai_supports_reasoning", False):
            settings.setdefault("openai_reasoning_summary", "auto")
        result = await super()._responses_create(messages, True, settings, model_request_parameters)
        return CompletedStream(result)
