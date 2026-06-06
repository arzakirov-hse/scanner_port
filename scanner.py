#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import smtplib
import ssl
import subprocess
from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib import parse, request

# Конфигурация приложения по умолчанию.
# Используется при автоматическом создании config.json.
DEFAULT_CONFIG: dict[str, Any] = {
    "network": "192.168.1.0/24",
    "ports": "21,22,25,80,110,143,443,445,3306,5432,6379,8080",
    "rate": 1000,
    "masscan_path": "masscan",
    "banner_timeout": 2.0,
    "concurrency": 200,
    "results_file": "results.json",
    "notifiers": {
        "telegram": {
            "enabled": False,
            "token": "",
            "chat_id": "",
        },
        "email": {
            "enabled": False,
            "smtp_host": "",
            "smtp_port": 465,
            "username": "",
            "password": "",
            "from_addr": "",
            "to_addrs": [],
            "use_tls": True,
        },
    },
}


@dataclass
# Класс хранения и проверки параметров конфигурации приложения.
class AppConfig:
    network: str
    ports: str
    rate: int
    masscan_path: str
    banner_timeout: float
    concurrency: int
    results_file: str
    notifiers: dict[str, Any] = field(default_factory=dict)

    # Создает объект конфигурации из словаря и выполняет проверку всех параметров.
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AppConfig":
        required = [
            "network",
            "ports",
            "rate",
            "masscan_path",
            "banner_timeout",
            "concurrency",
            "results_file",
            "notifiers",
        ]
        missing = [k for k in required if k not in data]
        if missing:
            raise ValueError(f"В config.json не хватает ключей: {', '.join(missing)}")

        if not isinstance(data["network"], str) or not data["network"].strip():
            raise ValueError("Поле 'network' должно быть непустой строкой")
        if not isinstance(data["ports"], str) or not data["ports"].strip():
            raise ValueError("Поле 'ports' должно быть непустой строкой")
        if not isinstance(data["rate"], int) or data["rate"] <= 0:
            raise ValueError("Поле 'rate' должно быть положительным целым числом")
        if not isinstance(data["banner_timeout"], (int, float)) or float(data["banner_timeout"]) <= 0:
            raise ValueError("Поле 'banner_timeout' должно быть положительным числом")
        if not isinstance(data["concurrency"], int) or data["concurrency"] <= 0:
            raise ValueError("Поле 'concurrency' должно быть положительным целым числом")
        if not isinstance(data["results_file"], str) or not data["results_file"].strip():
            raise ValueError("Поле 'results_file' должно быть непустой строкой")
        if not isinstance(data["notifiers"], dict):
            raise ValueError("Поле 'notifiers' должно быть объектом JSON")

        return cls(
            network=data["network"].strip(),
            ports=data["ports"].strip(),
            rate=int(data["rate"]),
            masscan_path=str(data["masscan_path"]).strip() or "masscan",
            banner_timeout=float(data["banner_timeout"]),
            concurrency=int(data["concurrency"]),
            results_file=data["results_file"].strip(),
            notifiers=data["notifiers"],
        )

# Создает файл config.json с настройками по умолчанию,
# если он отсутствует.
def ensure_config_file(path: Path) -> None:
    if not path.exists():
        path.write_text(
            json.dumps(DEFAULT_CONFIG, indent=4, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"[i] Создан файл {path} с настройками по умолчанию.")

# Загружает конфигурацию из файла и объединяет её
# со значениями по умолчанию.
def load_config(path: Path) -> AppConfig:
    ensure_config_file(path)

    with path.open("r", encoding="utf-8") as f:
        raw = json.load(f)

    merged = json.loads(json.dumps(DEFAULT_CONFIG))
    merged.update(raw)
    if isinstance(raw.get("notifiers"), dict):
        merged["notifiers"].update(raw["notifiers"])

    return AppConfig.from_dict(merged)

# Класс для хранения результатов предыдущих сканирований.
class ResultStorage:
    def __init__(self, path: Path):
        self.path = path

    # Загружает ранее сохраненные результаты сканирования.
    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}

        try:
            with self.path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            print("[!] Файл результатов поврежден. Создается новая база.")
            return {}

    # Сохраняет результаты сканирования в JSON-файл.
    def save(self, results: dict[str, Any]) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(results, f, indent=4, ensure_ascii=False)
        os.replace(tmp, self.path)

# Базовый интерфейс для всех типов уведомлений.
class Notifier:
    # Абстрактный метод отправки уведомления.
    def send(self, subject: str, message: str) -> None:
        raise NotImplementedError

# Отправка уведомлений через Telegram Bot API.
class TelegramNotifier(Notifier):
    # Инициализация параметров Telegram-бота.
    def __init__(self, token: str, chat_id: str):
        self.token = token.strip()
        self.chat_id = str(chat_id).strip()

    # Отправляет уведомление в Telegram-чат
    def send(self, subject: str, message: str) -> None:
        if not self.token or not self.chat_id:
            return

        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        data = parse.urlencode({"chat_id": self.chat_id, "text": message}).encode("utf-8")
        req = request.Request(url, data=data, method="POST")

        try:
            with request.urlopen(req, timeout=10) as resp:
                resp.read()
        except Exception as exc:
            print(f"[!] Telegram-уведомление не отправлено: {exc}")

# Отправка уведомлений по Email
class EmailNotifier(Notifier):
    def __init__(
        self,
        smtp_host: str,
        smtp_port: int,
        username: str,
        password: str,
        from_addr: str,
        to_addrs: list[str],
        use_tls: bool = True,
    ):
        self.smtp_host = smtp_host.strip()
        self.smtp_port = int(smtp_port)
        self.username = username.strip()
        self.password = password
        self.from_addr = from_addr.strip()
        self.to_addrs = [x.strip() for x in to_addrs if x.strip()]
        self.use_tls = use_tls

    def send(self, subject: str, message: str) -> None:
        if not (self.smtp_host and self.from_addr and self.to_addrs):
            return

        email = EmailMessage()
        email["From"] = self.from_addr
        email["To"] = ", ".join(self.to_addrs)
        email["Subject"] = subject
        email.set_content(message)

        try:
            if self.smtp_port == 465:
                context = ssl.create_default_context()
                with smtplib.SMTP_SSL(self.smtp_host, self.smtp_port, context=context, timeout=15) as server:
                    if self.username:
                        server.login(self.username, self.password)
                    server.send_message(email)
            else:
                with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=15) as server:
                    if self.use_tls:
                        context = ssl.create_default_context()
                        server.starttls(context=context)
                    if self.username:
                        server.login(self.username, self.password)
                    server.send_message(email)
        except Exception as exc:
            print(f"[!] Email-уведомление не отправлено: {exc}")

# Отправка уведомлений через несколько каналов
class MultiNotifier(Notifier):
    def __init__(self, notifiers: list[Notifier]):
        self.notifiers = notifiers

    def send(self, subject: str, message: str) -> None:
        for notifier in self.notifiers:
            notifier.send(subject, message)

# Работа с утилитой Masscan
class MasscanScanner:
    def __init__(self, masscan_path: str):
        self.masscan_path = masscan_path

    def ensure_masscan(self) -> None:
        if shutil.which(self.masscan_path) is None:
            raise FileNotFoundError(
                f"Masscan не найден: '{self.masscan_path}'. "
                f"Установите masscan и/или укажите корректный путь в config.json."
            )

    def run(self, network: str, ports: str, rate: int) -> str:
        self.ensure_masscan()
        command = [
            self.masscan_path,
            network,
            "-p",
            ports,
            "--rate",
            str(rate),
            "-oJ",
            "-",
        ]
        print("[i] Запуск Masscan...")
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"Masscan завершился с ошибкой:\n{result.stderr}")
        return result.stdout

    @staticmethod
    def parse(output: str) -> list[dict[str, Any]]:
        cleaned = "\n".join(
            line for line in output.splitlines() if not line.lstrip().startswith("#")
        ).strip()
        if not cleaned:
            return []

        data = json.loads(cleaned)
        hosts: list[dict[str, Any]] = []

        for item in data:
            ip = item.get("ip")
            for port_info in item.get("ports", []):
                hosts.append(
                    {
                        "ip": ip,
                        "port": int(port_info["port"]),
                    }
                )
        return hosts

# Асинхронное получение баннеров сервисов
class BannerGrabber:
    def __init__(self, timeout: float = 2.0, concurrency: int = 200):
        self.timeout = float(timeout)
        self.semaphore = asyncio.Semaphore(concurrency)

    async def get_banner(self, ip: str, port: int) -> str:
        async with self.semaphore:
            reader = None
            writer = None
            try:
                ssl_ctx = None
                if port == 443:
                    ssl_ctx = ssl._create_unverified_context()

                connect_coro = asyncio.open_connection(
                    host=ip,
                    port=port,
                    ssl=ssl_ctx,
                    server_hostname=ip if ssl_ctx else None,
                )
                reader, writer = await asyncio.wait_for(connect_coro, timeout=self.timeout)

                data = b""

                if port in (21, 25, 110, 143, 3306):
                    data += await self._safe_read(reader, 4096)

                    if port == 25:
                        await self._safe_write(writer, b"EHLO scanner.local\r\nQUIT\r\n")
                        data += await self._safe_read(reader, 4096)
                    elif port == 110:
                        await self._safe_write(writer, b"QUIT\r\n")
                    elif port == 143:
                        await self._safe_write(writer, b"1 LOGOUT\r\n")
                    elif port == 21:
                        await self._safe_write(writer, b"QUIT\r\n")

                elif port in (80, 8080, 443):
                    host_header = ip.encode("ascii", "ignore")
                    req = b"HEAD / HTTP/1.0\r\nHost: " + host_header + b"\r\nConnection: close\r\n\r\n"
                    await self._safe_write(writer, req)
                    data += await self._safe_read(reader, 8192)

                elif port == 6379:
                    await self._safe_write(writer, b"PING\r\n")
                    data += await self._safe_read(reader, 4096)

                elif port == 5432:
                    payload = b"user\x00postgres\x00database\x00postgres\x00\x00"
                    length = 4 + 4 + len(payload)
                    startup = length.to_bytes(4, "big") + (196608).to_bytes(4, "big") + payload
                    await self._safe_write(writer, startup)
                    data += await self._safe_read(reader, 4096)

                else:
                    data += await self._safe_read(reader, 4096)

                return data.decode(errors="ignore").strip()
            except Exception:
                return ""
            finally:
                if writer is not None:
                    writer.close()
                    with contextlib.suppress(Exception):
                        await writer.wait_closed()

    async def _safe_read(self, reader: asyncio.StreamReader, n: int) -> bytes:
        try:
            return await asyncio.wait_for(reader.read(n), timeout=self.timeout)
        except Exception:
            return b""

    async def _safe_write(self, writer: asyncio.StreamWriter, payload: bytes) -> None:
        try:
            writer.write(payload)
            await asyncio.wait_for(writer.drain(), timeout=self.timeout)
        except Exception:
            return

# Определение сервиса по баннеру и номеру порта
class ServiceDetector:
    PORT_SERVICES = {
        21: "FTP",
        22: "SSH",
        25: "SMTP",
        53: "DNS",
        80: "HTTP",
        110: "POP3",
        143: "IMAP",
        443: "HTTPS",
        445: "SMB",
        3306: "MySQL",
        3389: "RDP",
        5432: "PostgreSQL",
        6379: "Redis",
        8080: "HTTP",
    }

    @classmethod
    def detect(cls, port: int, banner: str) -> str:
        text = banner.lower()

        heuristics = [
            ("openssh", "OpenSSH"),
            ("apache", "Apache HTTP"),
            ("nginx", "Nginx"),
            ("iis", "Microsoft IIS"),
            ("redis", "Redis"),
            ("postgres", "PostgreSQL"),
            ("mysql", "MySQL"),
            ("mariadb", "MariaDB"),
            ("vsftpd", "vsftpd"),
            ("proftpd", "ProFTPD"),
            ("pure-ftpd", "Pure-FTPd"),
        ]

        for needle, service in heuristics:
            if needle in text:
                return service

        return cls.PORT_SERVICES.get(port, "Unknown")

# Основной класс сканера
class Scanner:
    def __init__(self, config: AppConfig):
        self.config = config
        self.storage = ResultStorage(Path(config.results_file))
        self.masscan = MasscanScanner(config.masscan_path)
        self.banner_grabber = BannerGrabber(config.banner_timeout, config.concurrency)
        self.notifier = self._build_notifier(config.notifiers)

    def _build_notifier(self, data: dict[str, Any]) -> Notifier:
        notifiers: list[Notifier] = []

        telegram_cfg = data.get("telegram", {}) or {}
        if telegram_cfg.get("enabled"):
            notifiers.append(
                TelegramNotifier(
                    telegram_cfg.get("token", ""),
                    telegram_cfg.get("chat_id", ""),
                )
            )

        email_cfg = data.get("email", {}) or {}
        if email_cfg.get("enabled"):
            notifiers.append(
                EmailNotifier(
                    smtp_host=email_cfg.get("smtp_host", ""),
                    smtp_port=int(email_cfg.get("smtp_port", 465)),
                    username=email_cfg.get("username", ""),
                    password=email_cfg.get("password", ""),
                    from_addr=email_cfg.get("from_addr", ""),
                    to_addrs=email_cfg.get("to_addrs", []) or [],
                    use_tls=bool(email_cfg.get("use_tls", True)),
                )
            )

        return MultiNotifier(notifiers)

    async def _collect_results(self, hosts: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        async def process(host: dict[str, Any]) -> Tuple[str, str, dict[str, Any]]:
            ip = str(host["ip"])
            port = int(host["port"])
            banner = await self.banner_grabber.get_banner(ip, port)
            service = ServiceDetector.detect(port, banner)
            return ip, str(port), {"service": service, "banner": banner}

        tasks = [process(host) for host in hosts]
        results: dict[str, dict[str, Any]] = {}

        for coro in asyncio.as_completed(tasks):
            ip, port, record = await coro
            results.setdefault(ip, {})[port] = record
            print(f"[+] {ip}:{port} -> {record['service']}")

        return results

    @staticmethod
    def find_new_ports(old_results: dict[str, Any], new_results: dict[str, Any]) -> list[tuple[str, str]]:
        new_ports: list[tuple[str, str]] = []

        for ip, ports in new_results.items():
            if ip not in old_results:
                new_ports.extend((ip, port) for port in ports.keys())
                continue

            old_ports = old_results.get(ip, {})
            for port in ports.keys():
                if port not in old_ports:
                    new_ports.append((ip, port))

        return new_ports

    def run(self) -> None:
        previous_results = self.storage.load()

        output = self.masscan.run(
            network=self.config.network,
            ports=self.config.ports,
            rate=self.config.rate,
        )
        hosts = self.masscan.parse(output)

        if not hosts:
            print("[i] Открытых портов не найдено.")
            self.storage.save(previous_results)
            return

        current_results = asyncio.run(self._collect_results(hosts))
        new_ports = self.find_new_ports(previous_results, current_results)

        if new_ports:
            subject = "Обнаружены новые открытые порты"
            message_lines = ["Обнаружены новые открытые порты:\n"]

            for ip, port in new_ports:
                service = current_results[ip][port]["service"]
                banner = current_results[ip][port]["banner"]
                message_lines.append(f"{ip}:{port} ({service})")
                if banner:
                    message_lines.append(f"  banner: {banner[:300]}")

            message = "\n".join(message_lines)
            print("\n" + message)
            self.notifier.send(subject, message)
        else:
            print("[i] Новых открытых портов не найдено.")

        self.storage.save(current_results)
        print(f"[i] Результаты сохранены в {self.config.results_file}")

# Точка входа в программу
def main() -> None:
    config_path = Path("config.json")
    config = load_config(config_path)
    scanner = Scanner(config)
    scanner.run()


if __name__ == "__main__":
    main()