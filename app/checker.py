import asyncio
import ipaddress
import socket
import ssl
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

import httpx


@dataclass
class CheckResult:
    status: str                  # "UP", "WARNING", ou "DOWN"
    latency_ms: Optional[int]    # Temps de réponse en millisecondes
    status_code: Optional[int]   # Code HTTP (ex: 200) ou None pour TCP/SSL
    ssl_days_left: Optional[int] # Jours restants avant expiration SSL
    error: Optional[str] = None  # Message d'erreur lisible si problème


def _is_safe_host(hostname: str) -> tuple[bool, Optional[str]]:
    """
    Protection anti-SSRF : résout le domaine et bloque toute IP privée,
    locale (loopback), link-local ou réservée.
    """
    if not hostname:
        return False, "Nom d'hôte vide ou invalide"

    blocked_names = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}
    if hostname.lower() in blocked_names:
        return False, f"Cible interdite (accès local bloqué) : {hostname}"

    try:
        addr_info = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return False, f"Résolution DNS impossible pour {hostname}"

    for entry in addr_info:
        ip_str = entry[4][0]
        try:
            ip_obj = ipaddress.ip_address(ip_str)
            if (
                ip_obj.is_private
                or ip_obj.is_loopback
                or ip_obj.is_link_local
                or ip_obj.is_multicast
                or ip_obj.is_reserved
                or ip_obj.is_unspecified
            ):
                return False, f"Sécurité SSRF : adresse IP non publique bloquée ({ip_str})"
        except ValueError:
            return False, f"Adresse IP invalide : {ip_str}"

    return True, None


async def _single_http_check(url: str, timeout: float = 10.0) -> CheckResult:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return CheckResult("DOWN", None, None, None, "Protocole non supporté (http/https uniquement)")

    hostname = parsed.hostname or ""
    safe, reason = _is_safe_host(hostname)
    if not safe:
        return CheckResult("DOWN", None, None, None, reason)

    start = time.perf_counter()
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            max_redirects=5,
            timeout=timeout,
            headers={"User-Agent": "SondeoOps-Monitor/1.0"},
        ) as client:
            response = await client.get(url)
            latency_ms = round((time.perf_counter() - start) * 1000)

            # Vérifie que la redirection finale n'a pas basculé vers une IP privée
            final_host = response.url.host
            final_safe, final_reason = _is_safe_host(final_host)
            if not final_safe:
                return CheckResult("DOWN", None, None, None, final_reason)

            if response.status_code < 400:
                return CheckResult("UP", latency_ms, response.status_code, None)
            return CheckResult(
                "DOWN",
                latency_ms,
                response.status_code,
                None,
                f"Code HTTP {response.status_code}",
            )
    except httpx.TimeoutException:
        return CheckResult("DOWN", None, None, None, f"Délai d'attente dépassé ({timeout}s)")
    except httpx.RequestError as exc:
        return CheckResult("DOWN", None, None, None, f"Erreur réseau : {type(exc).__name__}")


async def check_http(url: str, retries: int = 1, retry_delay: float = 3.0) -> CheckResult:
    """
    Sonde HTTP/HTTPS avec double contrôle automatique en cas d'échec
    pour éviter les faux positifs liés à une micro-coupure.
    """
    result = await _single_http_check(url)
    if result.status == "UP" or (result.error and "SSRF" in result.error):
        return result

    for _ in range(retries):
        await asyncio.sleep(retry_delay)
        result = await _single_http_check(url)
        if result.status == "UP":
            return result

    return result


async def check_tcp(host: str, port: int, timeout: float = 5.0, retries: int = 1) -> CheckResult:
    """
    Sonde TCP : vérifie qu'un port réseau accepte les connexions.
    """
    if not (1 <= port <= 65535):
        return CheckResult("DOWN", None, None, None, "Numéro de port invalide (1-65535)")

    safe, reason = _is_safe_host(host)
    if not safe:
        return CheckResult("DOWN", None, None, None, reason)

    for attempt in range(retries + 1):
        start = time.perf_counter()
        try:
            conn = asyncio.open_connection(host, port)
            _, writer = await asyncio.wait_for(conn, timeout=timeout)
            latency_ms = round((time.perf_counter() - start) * 1000)
            writer.close()
            await writer.wait_closed()
            return CheckResult("UP", latency_ms, None, None)
        except asyncio.TimeoutError:
            last_error = f"Timeout TCP sur {host}:{port}"
        except OSError as exc:
            last_error = f"Port {port} fermé ou injoignable ({exc.strerror or type(exc).__name__})"

        if attempt < retries:
            await asyncio.sleep(3.0)

    return CheckResult("DOWN", None, None, None, last_error)


def _sync_ssl_inspect(hostname: str, port: int = 443, timeout: float = 5.0) -> CheckResult:
    safe, reason = _is_safe_host(hostname)
    if not safe:
        return CheckResult("DOWN", None, None, None, reason)

    context = ssl.create_default_context()
    start = time.perf_counter()
    try:
        with socket.create_connection((hostname, port), timeout=timeout) as sock:
            with context.wrap_socket(sock, server_hostname=hostname) as ssock:
                latency_ms = round((time.perf_counter() - start) * 1000)
                cert = ssock.getpeercert()

        not_after_str = cert.get("notAfter")
        if not not_after_str:
            return CheckResult("DOWN", latency_ms, None, None, "Date d'expiration SSL introuvable")

        expires_at = datetime.strptime(not_after_str, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
        days_left = (expires_at - datetime.now(timezone.utc)).days

        if days_left < 0:
            return CheckResult("DOWN", latency_ms, None, days_left, "Certificat SSL expiré")
        if days_left <= 30:
            return CheckResult("WARNING", latency_ms, None, days_left, f"Expire dans {days_left} jours")
        return CheckResult("UP", latency_ms, None, days_left)

    except ssl.SSLCertVerificationError as exc:
        return CheckResult("DOWN", None, None, None, f"Certificat SSL invalide : {exc.reason}")
    except (socket.timeout, TimeoutError):
        return CheckResult("DOWN", None, None, None, f"Timeout SSL sur {hostname}:{port}")
    except OSError as exc:
        return CheckResult("DOWN", None, None, None, f"Connexion SSL impossible : {exc.strerror or type(exc).__name__}")


async def check_ssl(hostname: str, port: int = 443) -> CheckResult:
    """
    Exécute l'inspection du certificat TLS sans bloquer la boucle asynchrone.
    """
    return await asyncio.to_thread(_sync_ssl_inspect, hostname, port)


# Petit test rapide en ligne de commande : python3 -m app.checker
async def _demo():
    print("--- Test des sondes SondeoOps ---")
    print("1. HTTP Google :", await check_http("https://www.google.com"))
    print("2. SSL GitHub  :", await check_ssl("github.com"))
    print("3. TCP DNS     :", await check_tcp("1.1.1.1", 53))
    print("4. Test SSRF   :", await check_http("http://127.0.0.1:8000"))


if __name__ == "__main__":
    asyncio.run(_demo())
