#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import socket
import subprocess
import requests


# =====================================
# Загрузка конфигурации
# =====================================

def load_config():
    """
    Загружает настройки из config.json
    """

    with open("config.json", "r", encoding="utf-8") as file:
        return json.load(file)


# =====================================
# Загрузка предыдущих результатов
# =====================================

def load_previous_results():
    """
    Загружает результаты прошлого сканирования.

    Если файл отсутствует,
    возвращает пустой словарь.
    """

    try:
        with open("results.json", "r", encoding="utf-8") as file:
            return json.load(file)

    except FileNotFoundError:
        return {}


# =====================================
# Сохранение результатов
# =====================================

def save_results(results):
    """
    Сохраняет результаты сканирования.
    """

    with open("results.json", "w", encoding="utf-8") as file:
        json.dump(results, file, indent=4)


# =====================================
# Запуск Masscan
# =====================================

def run_masscan(network, ports, rate):
    """
    Запускает Masscan и возвращает результат.
    """

    command = [
        "masscan",
        network,
        "-p",
        ports,
        "--rate",
        str(rate),
        "-oJ",
        "-"
    ]

    print("Запуск Masscan...")

    result = subprocess.run(
        command,
        capture_output=True,
        text=True
    )

    if result.returncode != 0:
        raise Exception(result.stderr)

    return result.stdout


# =====================================
# Разбор вывода Masscan
# =====================================

def parse_masscan(output):
    """
    Преобразует JSON Masscan
    в список найденных портов.
    """

    output = output.strip()

    if not output:
        return []

    data = json.loads(output)

    hosts = []

    for item in data:

        ip = item["ip"]

        for port_info in item["ports"]:

            hosts.append({
                "ip": ip,
                "port": port_info["port"]
            })

    return hosts


# =====================================
# Получение баннера
# =====================================

def get_banner(ip, port):
    """
    Пытается получить баннер сервиса.
    """

    try:

        sock = socket.socket()
        sock.settimeout(2)

        sock.connect((ip, port))

        if port in [80, 8080]:
            sock.send(b"HEAD / HTTP/1.0\r\n\r\n")

        elif port == 25:
            sock.send(b"EHLO test\r\n")

        elif port == 110:
            sock.send(b"QUIT\r\n")

        data = sock.recv(1024)

        sock.close()

        return data.decode(errors="ignore").strip()

    except:
        return ""


# =====================================
# Определение сервиса
# =====================================

def detect_service(port, banner):
    """
    Определяет сервис по порту
    и содержимому баннера.
    """

    services = {
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
        8080: "HTTP"
    }

    banner_lower = banner.lower()

    if "openssh" in banner_lower:
        return "OpenSSH"

    if "apache" in banner_lower:
        return "Apache HTTP"

    if "nginx" in banner_lower:
        return "Nginx"

    if "iis" in banner_lower:
        return "Microsoft IIS"

    if "redis" in banner_lower:
        return "Redis"

    return services.get(port, "Unknown")


# =====================================
# Формирование структуры результатов
# =====================================

def build_results(hosts):
    """
    Создаёт словарь результатов:
    IP -> порт -> сервис
    """

    results = {}

    for host in hosts:

        ip = host["ip"]
        port = str(host["port"])

        banner = get_banner(ip, host["port"])

        service = detect_service(
            host["port"],
            banner
        )

        if ip not in results:
            results[ip] = {}

        results[ip][port] = {
            "service": service,
            "banner": banner
        }

        print(
            f"[+] {ip}:{port} -> {service}"
        )

    return results


# =====================================
# Поиск новых портов
# =====================================

def find_new_ports(old_results, new_results):
    """
    Находит новые порты,
    которых не было ранее.
    """

    new_ports = []

    for ip in new_results:

        if ip not in old_results:

            for port in new_results[ip]:

                new_ports.append(
                    (ip, port)
                )

            continue

        for port in new_results[ip]:

            if port not in old_results[ip]:

                new_ports.append(
                    (ip, port)
                )

    return new_ports


# =====================================
# Telegram уведомление
# =====================================

def send_telegram(token, chat_id, message):
    """
    Отправляет сообщение в Telegram.
    """

    url = (
        f"https://api.telegram.org/"
        f"bot{token}/sendMessage"
    )

    requests.post(
        url,
        data={
            "chat_id": chat_id,
            "text": message
        },
        timeout=10
    )


# =====================================
# Главная функция
# =====================================

def main():

    config = load_config()

    previous_results = load_previous_results()

    masscan_output = run_masscan(
        config["network"],
        config["ports"],
        config["rate"]
    )

    hosts = parse_masscan(
        masscan_output
    )

    current_results = build_results(
        hosts
    )

    new_ports = find_new_ports(
        previous_results,
        current_results
    )

    if new_ports:

        message = (
            "Обнаружены новые открытые порты:\n\n"
        )

        for ip, port in new_ports:

            service = current_results[ip][port]["service"]

            message += (
                f"{ip}:{port} "
                f"({service})\n"
            )

        print("\n" + message)

        send_telegram(
            config["telegram_token"],
            config["telegram_chat_id"],
            message
        )

    else:
        print(
            "\nНовых открытых портов не найдено."
        )

    save_results(
        current_results
    )


if __name__ == "__main__":
    main()