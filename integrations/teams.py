# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Microsoft Teams Bot Framework integration.

Handles incoming webhooks from Azure Bot Service, JWT validation,
token management for outgoing messages, and Activity parsing.

Architecture:
- Azure Bot Service acts as relay (free tier, no hosting cost)
- Incoming: POST /api/teams-messages with Activity JSON + JWT in Authorization header
- Outgoing: REST calls to serviceUrl with bot token (client credentials flow)
- Token: Azure AD client credentials (app_id + app_secret)

Wire protocol: Bot Framework REST API v3
- Send:   POST   {serviceUrl}/v3/conversations/{convId}/activities
- Update: PUT    {serviceUrl}/v3/conversations/{convId}/activities/{actId}
- Delete: DELETE {serviceUrl}/v3/conversations/{convId}/activities/{actId}

No SDK dependency — raw REST with PyJWT for JWT validation.
"""

import asyncio
import json
import logging
import time
from typing import Any

import httpx
import jwt  # PyJWT[crypto]
from jwt.algorithms import RSAAlgorithm

log = logging.getLogger(__name__)

# ============================================================
# Constants
# ============================================================

# OpenID metadata for JWT validation (Bot Framework v3.2+)
_OPENID_METADATA_URL = (
    "https://login.botframework.com/v1/.well-known/openidconfiguration"
)

# Token endpoint template for outgoing messages (single-tenant)
_TOKEN_URL_TEMPLATE = (
    "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
)

# Bot Framework scope for outgoing REST calls
_BOT_FRAMEWORK_SCOPE = "https://api.botframework.com/.default"

# Valid JWT issuers for incoming webhooks
# Single-tenant issuers are checked dynamically against tenant_id
_VALID_ISSUERS_STATIC = [
    "https://api.botframework.com",
]

# Cache durations
_JWKS_CACHE_TTL_S = 3600        # 1h — Microsoft recommends short cache periods
_JWKS_MAX_STALE_S = 172800      # 48h — reject stale cache beyond this
_TOKEN_REFRESH_BUFFER_S = 300   # refresh bot token 5 min before expiry

# HTTP timeout for Bot Framework REST calls
_HTTP_TIMEOUT_S = 30


# ============================================================
# TeamsClient
# ============================================================

class TeamsClient:
    """Microsoft Teams Bot Framework client.

    Handles:
    - JWT validation of incoming webhooks (RS256, JWKS from login.botframework.com)
    - Client credentials token acquisition for outgoing messages
    - Send/update/delete activities via Bot Framework REST API
    - Activity parsing to standard bot parsed dict format
    """

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        tenant_id: str,
        allowed_tenants: list[str] | None = None,
    ):
        self.app_id = app_id
        self.app_secret = app_secret
        self.tenant_id = tenant_id

        # Build allowed tenant set (always includes own tenant)
        self.allowed_tenants: set[str] = set(allowed_tenants or [])
        if tenant_id:
            self.allowed_tenants.add(tenant_id)

        # JWKS cache: kid -> JWK dict
        self._jwks: dict[str, dict] = {}
        self._jwks_fetched_at: float = 0
        self._jwks_lock = asyncio.Lock()

        # Bot token cache (client credentials)
        self._bot_token: str = ""
        self._bot_token_expires_at: float = 0
        self._token_lock = asyncio.Lock()

        # Conversation serviceUrl mapping (conversation_id -> serviceUrl)
        # Populated from incoming activities, used for outgoing messages.
        # In-memory only — repopulated on first message after restart.
        self._service_urls: dict[str, str] = {}

        # HTTP client (shared, connection-pooling)
        self._http: httpx.AsyncClient | None = None

    async def _get_http(self) -> httpx.AsyncClient:
        """Get or create the shared HTTP client."""
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(timeout=_HTTP_TIMEOUT_S)
        return self._http

    async def close(self):
        """Close the HTTP client."""
        if self._http and not self._http.is_closed:
            await self._http.aclose()
            self._http = None

    # ============================================================
    # JWT Validation (incoming webhooks)
    # ============================================================

    async def _fetch_jwks(self, force: bool = False) -> dict[str, dict]:
        """Fetch JWKS keys from Bot Framework OpenID metadata.

        Keys are cached for _JWKS_CACHE_TTL_S. On cache miss or force=True,
        fetches fresh metadata → JWKS URI → key set.

        Returns: dict mapping kid -> JWK dict.
        Raises: Exception on network/parse failure (falls back to stale cache).
        """
        async with self._jwks_lock:
            now = time.time()
            if (not force and self._jwks
                    and (now - self._jwks_fetched_at) < _JWKS_CACHE_TTL_S):
                return self._jwks

            http = await self._get_http()
            try:
                # Step 1: OpenID discovery metadata
                resp = await http.get(_OPENID_METADATA_URL)
                resp.raise_for_status()
                metadata = resp.json()
                jwks_uri = metadata.get("jwks_uri")
                if not jwks_uri:
                    raise ValueError("OpenID metadata missing jwks_uri")

                # Step 2: Fetch JWKS
                resp = await http.get(jwks_uri)
                resp.raise_for_status()
                jwks_data = resp.json()

                # Build kid -> JWK lookup
                keys: dict[str, dict] = {}
                for key_data in jwks_data.get("keys", []):
                    kid = key_data.get("kid")
                    if kid:
                        keys[kid] = key_data

                self._jwks = keys
                self._jwks_fetched_at = now
                log.info("Teams JWKS refreshed: %d keys cached", len(keys))
                return keys

            except Exception as e:
                log.error("Failed to fetch Teams JWKS: %s", e, exc_info=True)
                stale_age = now - self._jwks_fetched_at
                if self._jwks and stale_age < _JWKS_MAX_STALE_S:
                    log.warning(
                        "Using stale JWKS cache (%d keys, %.0fh old)",
                        len(self._jwks), stale_age / 3600,
                    )
                    return self._jwks
                if self._jwks:
                    log.error(
                        "JWKS cache too stale (%.0fh) — rejecting",
                        stale_age / 3600,
                    )
                raise

    async def validate_jwt(self, auth_header: str) -> dict:
        """Validate JWT from incoming Bot Framework webhook.

        Args:
            auth_header: Full Authorization header value ("Bearer <token>")

        Returns: Decoded token claims dict on success.
        Raises: ValueError on any validation failure.
        """
        if not auth_header or not auth_header.startswith("Bearer "):
            raise ValueError("Missing or invalid Authorization header")

        token = auth_header[7:]  # Strip "Bearer "

        # Decode header to extract kid (key ID) — unverified at this point
        try:
            unverified_header = jwt.get_unverified_header(token)
        except jwt.exceptions.DecodeError as e:
            raise ValueError(f"Invalid JWT header: {e}")

        kid = unverified_header.get("kid")
        if not kid:
            raise ValueError("JWT missing 'kid' in header")

        alg = unverified_header.get("alg", "RS256")
        if alg != "RS256":
            raise ValueError(f"Unsupported JWT algorithm: {alg}")

        # Get signing key from JWKS
        keys = await self._fetch_jwks()
        key_data = keys.get(kid)

        if not key_data:
            # Key not found — force refresh (key rotation may have occurred)
            keys = await self._fetch_jwks(force=True)
            key_data = keys.get(kid)
            if not key_data:
                log.warning("JWT kid '%s' not found in JWKS after refresh", kid)
                raise ValueError("JWT signing key not found")

        # Build RSA public key from JWK
        try:
            public_key = RSAAlgorithm.from_jwk(json.dumps(key_data))
        except Exception as e:
            raise ValueError(f"Failed to build public key from JWK: {e}")

        # Build valid issuers (static + tenant-specific)
        valid_issuers = list(_VALID_ISSUERS_STATIC)
        if self.tenant_id:
            valid_issuers.append(
                f"https://login.microsoftonline.com/{self.tenant_id}/v2.0"
            )
            valid_issuers.append(
                f"https://sts.windows.net/{self.tenant_id}/"
            )

        # Validate and decode token
        try:
            claims = jwt.decode(
                token,
                public_key,
                algorithms=["RS256"],
                audience=self.app_id,
                issuer=valid_issuers,
                options={
                    "verify_exp": True,
                    "verify_iat": True,
                    "verify_aud": True,
                    "verify_iss": True,
                },
            )
        except jwt.ExpiredSignatureError:
            raise ValueError("JWT expired")
        except jwt.InvalidAudienceError:
            log.warning("JWT audience mismatch (expected %s)", self.app_id[:8])
            raise ValueError("JWT audience mismatch")
        except jwt.InvalidIssuerError as e:
            raise ValueError(f"JWT issuer mismatch: {e}")
        except jwt.PyJWTError as e:
            raise ValueError(f"JWT validation failed: {e}")

        # Tenant allowlist check
        token_tenant = claims.get("tid", "")
        if self.allowed_tenants and token_tenant not in self.allowed_tenants:
            raise ValueError(
                f"JWT tenant '{token_tenant}' not in allowed tenants"
            )

        return claims

    # ============================================================
    # Bot Token (outgoing messages — client credentials flow)
    # ============================================================

    async def _get_bot_token(self) -> str:
        """Get bot token for outgoing REST calls.

        Uses Azure AD client credentials flow:
        POST to login.microsoftonline.com/{tenant}/oauth2/v2.0/token
        with client_id, client_secret, scope=api.botframework.com/.default

        Token is cached until 5 min before expiry.

        Returns: Access token string.
        Raises: Exception on auth failure.
        """
        async with self._token_lock:
            now = time.time()
            if (self._bot_token
                    and now < (self._bot_token_expires_at
                               - _TOKEN_REFRESH_BUFFER_S)):
                return self._bot_token

            http = await self._get_http()
            token_url = _TOKEN_URL_TEMPLATE.format(tenant_id=self.tenant_id)

            try:
                resp = await http.post(
                    token_url,
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self.app_id,
                        "client_secret": self.app_secret,
                        "scope": _BOT_FRAMEWORK_SCOPE,
                    },
                )
                resp.raise_for_status()
                data = resp.json()

                self._bot_token = data["access_token"]
                expires_in = data.get("expires_in", 3600)
                self._bot_token_expires_at = now + expires_in
                log.info("Teams bot token refreshed, expires_in=%ss", expires_in)
                return self._bot_token

            except httpx.HTTPStatusError as e:
                try:
                    err = e.response.json()
                    err_msg = f"{err.get('error', '?')}: {err.get('error_description', '?')[:80]}"
                except Exception:
                    err_msg = f"HTTP {e.response.status_code}"
                log.error(
                    "Teams token request failed: %s %s",
                    e.response.status_code,
                    err_msg,
                    exc_info=True,
                )
                raise
            except Exception as e:
                log.error("Teams token request error: %s", e, exc_info=True)
                raise

    # ============================================================
    # Conversation References
    # ============================================================

    @staticmethod
    def _validate_service_url(service_url: str) -> bool:
        """Validate serviceUrl to prevent SSRF.

        Bot Framework serviceUrls are always HTTPS on Microsoft-owned domains.
        Block non-HTTPS, private IPs, and Docker-internal hostnames.
        """
        if not service_url:
            return False
        # Must be HTTPS
        if not service_url.startswith("https://"):
            return False
        # Extract hostname
        try:
            from urllib.parse import urlparse
            parsed = urlparse(service_url)
            hostname = (parsed.hostname or "").lower()
        except Exception:
            return False
        if not hostname:
            return False
        # Block private/internal hostnames
        _blocked = (
            "localhost", "127.0.0.1", "0.0.0.0",
            "camofox-browser", "wa-bridge", "bot-core",
        )
        if hostname in _blocked:
            return False
        # Block private IP ranges (10.x, 172.16-31.x, 192.168.x, 169.254.x)
        import ipaddress
        try:
            ip = ipaddress.ip_address(hostname)
            if ip.is_private or ip.is_loopback or ip.is_link_local:
                return False
        except ValueError:
            pass  # Not an IP literal — that's fine (it's a domain name)
        return True

    def store_service_url(self, conversation_id: str, service_url: str):
        """Store/update the serviceUrl for a conversation.

        Called on every incoming activity — serviceUrl can change between
        messages (Azure Bot Service load-balances across regions).

        Validates URL to prevent SSRF: must be HTTPS, no private IPs,
        no Docker-internal hostnames.
        """
        if not conversation_id or not service_url:
            return
        if not self._validate_service_url(service_url):
            log.warning(
                "Teams: rejecting invalid serviceUrl: %s",
                service_url[:60],
            )
            return
        # Cap size to prevent memory exhaustion
        if len(self._service_urls) >= 10000:
            # Evict oldest entry
            oldest = next(iter(self._service_urls))
            del self._service_urls[oldest]
        self._service_urls[conversation_id] = service_url

    def get_service_url(self, conversation_id: str) -> str | None:
        """Get the serviceUrl for a conversation (None if unknown)."""
        return self._service_urls.get(conversation_id)

    # ============================================================
    # Bot Framework REST API — Send / Update / Delete
    # ============================================================

    async def send_activity(
        self,
        conversation_id: str,
        activity: dict,
    ) -> dict:
        """Send an Activity to a conversation.

        Args:
            conversation_id: Teams conversation ID
            activity: Activity JSON dict (type, text, etc.)

        Returns: {"id": "activity_id"} on success.
        Raises: ValueError if no serviceUrl, httpx errors on API failure.
        """
        service_url = self._service_urls.get(conversation_id)
        if not service_url:
            raise ValueError(
                f"No serviceUrl for conversation {conversation_id[:30]}... "
                f"(bot hasn't received a message from this conversation yet)"
            )

        token = await self._get_bot_token()
        http = await self._get_http()

        url = (f"{service_url.rstrip('/')}/v3/conversations/"
               f"{conversation_id}/activities")

        try:
            resp = await http.post(
                url,
                json=activity,
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()
            result = resp.json()
            log.debug("Teams send_activity ok: conversation=%s activity_id=%s",
                      conversation_id[:20], result.get("id", "?"))
            return result

        except httpx.HTTPStatusError as e:
            log.error(
                "Teams send_activity failed: status=%s conversation=%s",
                e.response.status_code,
                conversation_id[:20],
            )
            raise
        except Exception as e:
            log.error("Teams send_activity error: %s", e, exc_info=True)
            raise

    async def update_activity(
        self,
        conversation_id: str,
        activity_id: str,
        activity: dict,
    ) -> dict:
        """Update an existing Activity (for placeholder/streaming pattern).

        Args:
            conversation_id: Teams conversation ID
            activity_id: ID of the activity to update
            activity: Updated Activity JSON dict

        Returns: API response dict.
        """
        service_url = self._service_urls.get(conversation_id)
        if not service_url:
            raise ValueError(
                f"No serviceUrl for conversation {conversation_id[:30]}..."
            )

        token = await self._get_bot_token()
        http = await self._get_http()

        url = (f"{service_url.rstrip('/')}/v3/conversations/"
               f"{conversation_id}/activities/{activity_id}")

        # Include activity_id in body (required by some serviceUrls)
        # Copy to avoid mutating caller's dict
        activity = {**activity, "id": activity_id}

        try:
            resp = await http.put(
                url,
                json=activity,
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()
            return resp.json()

        except httpx.HTTPStatusError as e:
            log.error(
                "Teams update_activity failed: status=%s",
                e.response.status_code,
            )
            raise
        except Exception as e:
            log.error("Teams update_activity error: %s", e, exc_info=True)
            raise

    async def delete_activity(
        self,
        conversation_id: str,
        activity_id: str,
    ) -> None:
        """Delete an Activity.

        Silently succeeds if already deleted (404).
        """
        service_url = self._service_urls.get(conversation_id)
        if not service_url:
            log.warning("Teams delete: no serviceUrl for %s",
                        conversation_id[:20])
            return

        token = await self._get_bot_token()
        http = await self._get_http()

        url = (f"{service_url.rstrip('/')}/v3/conversations/"
               f"{conversation_id}/activities/{activity_id}")

        try:
            resp = await http.delete(
                url,
                headers={"Authorization": f"Bearer {token}"},
            )
            # 404 = already deleted, that's fine
            if resp.status_code != 404:
                resp.raise_for_status()

        except httpx.HTTPStatusError as e:
            if e.response.status_code != 404:
                log.error("Teams delete_activity failed: %s",
                          e.response.status_code)
                raise
        except Exception as e:
            log.error("Teams delete_activity error: %s", e, exc_info=True)
            raise

    # ============================================================
    # Convenience Methods
    # ============================================================

    async def send_message(
        self,
        conversation_id: str,
        text: str,
        text_format: str = "markdown",
        reply_to_id: str | None = None,
    ) -> dict:
        """Send a text message to a conversation.

        Args:
            conversation_id: Teams conversation ID
            text: Message text (markdown supported)
            text_format: "markdown" or "plain"
            reply_to_id: Activity ID to reply to (optional threading)

        Returns: {"id": "activity_id"}
        """
        activity: dict[str, Any] = {
            "type": "message",
            "text": text,
            "textFormat": text_format,
        }
        if reply_to_id:
            activity["replyToId"] = reply_to_id
        return await self.send_activity(conversation_id, activity)

    async def update_message(
        self,
        conversation_id: str,
        activity_id: str,
        text: str,
        text_format: str = "markdown",
    ) -> dict:
        """Update a message (for placeholder/streaming pattern).

        Teams supports UpdateActivity — edit message content in-place.
        Works in all scopes (personal, group chat, channel).
        """
        activity = {
            "type": "message",
            "text": text,
            "textFormat": text_format,
        }
        return await self.update_activity(
            conversation_id, activity_id, activity
        )

    async def send_typing(self, conversation_id: str) -> None:
        """Send typing indicator.

        Non-critical — failures are silently swallowed.
        """
        try:
            await self.send_activity(conversation_id, {"type": "typing"})
        except Exception as e:
            log.debug("Teams typing indicator failed: %s", e)

    # ============================================================
    # Activity Parsing
    # ============================================================

    def parse_activity(self, body: dict) -> dict | None:
        """Parse incoming Activity JSON to standard bot parsed dict.

        Handles:
        - message activities → parsed dict for pipeline
        - conversationUpdate (bot added) → registers conversation
        - Other types → logged and ignored

        Returns:
            Parsed dict for pipeline processing, or None for non-message
            activities that should be silently handled/ignored.
        """
        activity_type = body.get("type", "")
        conversation = body.get("conversation", {})
        conversation_id = conversation.get("id", "")
        service_url = body.get("serviceUrl", "")
        sender = body.get("from", {})
        channel_data = body.get("channelData", {})
        tenant = channel_data.get("tenant", {})

        # Always store serviceUrl from incoming activities
        if conversation_id and service_url:
            self.store_service_url(conversation_id, service_url)

        # Handle conversationUpdate (bot install/uninstall)
        if activity_type == "conversationUpdate":
            members_added = body.get("membersAdded", [])
            recipient_id = body.get("recipient", {}).get("id", "")
            for member in members_added:
                if member.get("id") == recipient_id:
                    log.info(
                        "Teams: bot installed in conversation %s "
                        "(type=%s, tenant=%s)",
                        conversation_id[:30],
                        conversation.get("conversationType", "?"),
                        tenant.get("id", "?")[:8],
                    )
            return None

        # Handle SSO token exchange (C5 fix — per-user OBO flow)
        if activity_type == "invoke":
            invoke_name = body.get("name", "")
            if invoke_name == "signin/tokenExchange":
                token_data = body.get("value", {})
                sso_token = token_data.get("token", "")
                user_aad_id = sender.get("aadObjectId", "")
                user_name_str = sender.get("name", "")
                if sso_token and user_aad_id:
                    log.info("Teams SSO token exchange for user %s", user_aad_id[:8])
                    try:
                        import asyncio
                        from bot.auth.token_vault import exchange_teams_token
                        asyncio.create_task(exchange_teams_token(
                            teams_token=sso_token,
                            user_id=user_aad_id,
                            user_name=user_name_str,
                        ))
                    except Exception as e:
                        log.error("Teams SSO exchange failed: %s", e)
                # Return sentinel for invoke response (Teams requires {"status": 200} body)
                return {"_invoke_response": True}
            return None  # other invoke types — silent

        # Only process message activities
        if activity_type != "message":
            log.debug("Teams: ignoring activity type '%s' from %s",
                      activity_type, conversation_id[:20])
            return None

        text = body.get("text", "").strip()

        # Remove bot @mention text from message
        # Teams includes "<at>BotName</at>" in text when bot is @mentioned
        # in channels/group chats. Strip it for clean processing.
        entities = body.get("entities", [])
        for entity in entities:
            if entity.get("type") == "mention":
                mentioned = entity.get("mentioned", {})
                # Check if the mentioned entity is the bot itself
                mentioned_id = mentioned.get("id", "")
                if mentioned_id == body.get("recipient", {}).get("id", ""):
                    mention_text = entity.get("text", "")
                    if mention_text:
                        text = text.replace(mention_text, "").strip()

        # Handle attachments (presence flag only — full handling in v2)
        attachments = body.get("attachments", [])
        has_media = bool(attachments)

        # Determine conversation type
        conv_type = conversation.get("conversationType", "")
        # conversationType: "personal" (1:1), "groupChat", "channel"

        # Build standard parsed dict
        parsed: dict[str, Any] = {
            "source": "teams",
            "chat_id": conversation_id,
            "user_id": sender.get("aadObjectId", sender.get("id", "")),
            "user_name": sender.get("name", "Unknown"),
            "text": text,
            "message_id": body.get("id", ""),
            "raw": body,
            "has_media": has_media,
            "is_external_chat": True,
            "is_active_plus": True,
            "reply_to": None,
            # Teams-specific fields (stored for routing/reference)
            "teams_service_url": service_url,
            "teams_tenant_id": tenant.get("id", ""),
            "teams_conversation_type": conv_type,
        }

        return parsed


# ============================================================
# Module-level client management
# ============================================================

_client: TeamsClient | None = None


def get_client() -> TeamsClient | None:
    """Get the global TeamsClient instance (None if not initialized)."""
    return _client


def init_client(
    app_id: str,
    app_secret: str,
    tenant_id: str,
    allowed_tenants: list[str] | None = None,
) -> TeamsClient:
    """Initialize the global TeamsClient.

    Called from main.py lifespan when TEAMS_ENABLED=true.
    """
    global _client
    _client = TeamsClient(app_id, app_secret, tenant_id, allowed_tenants)
    log.info(
        "Teams client initialized: app_id=%s..., tenant=%s...",
        app_id[:8], tenant_id[:8],
    )
    return _client


async def close_client() -> None:
    """Close the global TeamsClient. Called during shutdown."""
    global _client
    if _client:
        await _client.close()
        _client = None
        log.info("Teams client closed")
