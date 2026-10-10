# -*- coding: cp1252 -*-
import yaml
import json
import requests
import asyncio

class Student:
    def __init__(self, name, mac):
        self.name = name
        self.mac = mac


class Service:
    def __init__(self, name, protocol, port):
        self.name = name
        self.protocol = protocol
        self.port = port


class Server:
    def __init__(self, name, ip, services):
        self.name = name
        self.ip = ip
        self.services = [
            Service(service["name"], service["protocol"], service["port"])
            for service in services
        ]


class Course:
    def __init__(self, name, code, state, students, servers):
        self.name = name
        self.code = code
        self.state = state
        self.students = students
        self.servers = servers


def load_data(filename):
    with open(filename, "r") as file:
        data = yaml.safe_load(file)

    students = [
        Student(x["name"], x["mac"])
        for x in data["students"]
    ]

    servers = [
        Server(y["name"], y["ip"], y["services"])
        for y in data["servers"]
    ]

    courses = [
        Course(
            z["name"],
            z["code"],
            z["state"],
            z["students"],
            z["servers"]
        )
        for z in data["courses"]
    ]

    return students, servers, courses

def get_attachment_point(controller_ip, mac):
    endpoint = '/wm/device/'
    url = 'http://' + controller_ip + ':8080' + endpoint
    response = requests.get(
        url,
        params={'mac': mac}
    )
    result, = response.json()
    attachment_points = result.get("attachmentPoint", [])

    if not attachment_points:
        return None, None

    attachment_point, = attachment_points
    return attachment_point["switchDPID"], attachment_point["port"]


def get_route(controller_ip, src_mac, dst_mac):
    src_sw_dpid, src_sw_port = get_attachment_point(controller_ip, src_mac)
    dst_sw_dpid, dst_sw_port = get_attachment_point(controller_ip, dst_mac)
    endpoint = f'/wm/topology/route/{src_sw_dpid}/{src_sw_port}/{dst_sw_dpid}/{dst_sw_port}/json'
    url = 'http://' + controller_ip + ':8080' + endpoint
    response = requests.get(url)
    return response.json()


def menu():
    print("\n" + "#" * 50)
    print("Network Policy Manager")
    print("#" * 50)
    print("1) Importar")
    print("2) Exportar")
    print("3) Cursos")
    print("4) Alumnos")
    print("5) Servidores")
    print("6) Políticas")
    print("7) Conexiones")
    print("8) Salir")

    return input(">>> ").strip()

def get_device(controller_ip, **params):
    url = f"http://{controller_ip}:8080/wm/device/"
    response = requests.get(url, params=params, timeout=5)
    response.raise_for_status()

    devices = response.json()

    if not devices:
        raise ValueError("Dispositivo no detectado por Floodlight.")

    return devices[0]


def get_port_number(route_item):
    port = route_item["port"]

    if isinstance(port, dict):
        return port["portNumber"]

    return port


async def send_flow(controller_ip, flow):
    url = (
        f"http://{controller_ip}:8080"
        "/wm/staticflowpusher/json"
    )

    response = await asyncio.to_thread(
        requests.post,
        url,
        json=flow,
        timeout=5
    )

    if not response.ok:
        raise RuntimeError(
            f"Error al insertar {flow['name']}: "
            f"{response.status_code} {response.text}"
        )


async def remove_flow(controller_ip, flow_name):
    url = (
        f"http://{controller_ip}:8080"
        "/wm/staticflowpusher/json"
    )

    response = await asyncio.to_thread(
        requests.delete,
        url,
        json={"name": flow_name},
        timeout=5
    )

    if not response.ok:
        raise RuntimeError(
            f"Error al eliminar {flow_name}: "
            f"{response.status_code} {response.text}"
        )


async def build_route(
    controller_ip,
    student,
    server,
    service,
    handler
):
    student_device = get_device(
        controller_ip,
        mac=student.mac
    )

    server_device = get_device(
        controller_ip,
        ipv4=server.ip
    )

    student_ip = student_device["ipv4"][0]
    server_mac = server_device["mac"][0]

    forward_route = get_route(
        controller_ip,
        student.mac,
        server_mac
    )

    reverse_route = get_route(
        controller_ip,
        server_mac,
        student.mac
    )

    if not forward_route or not reverse_route:
        raise ValueError(
            "Floodlight no pudo calcular la ruta."
        )

    protocol = service.protocol.upper()

    if protocol == "TCP":
        ip_protocol = "0x06"
        forward_port_field = "tcp_dst"
        reverse_port_field = "tcp_src"

    elif protocol == "UDP":
        ip_protocol = "0x11"
        forward_port_field = "udp_dst"
        reverse_port_field = "udp_src"

    else:
        raise ValueError("Protocolo no soportado.")

    flows = []

    def create_direction(
        route,
        direction,
        src_mac,
        dst_mac,
        src_ip,
        dst_ip,
        port_field
    ):
        for index in range(0, len(route), 2):
            entry = route[index]
            exit_entry = route[index + 1]

            switch = entry["switch"]
            input_port = get_port_number(entry)
            output_port = get_port_number(exit_entry)

            flow_number = index // 2 + 1

            ip_name = (
                f"{handler}-{direction}-ip-{flow_number}"
            )

            ip_flow = {
                "switch": switch,
                "name": ip_name,
                "cookie": "0",
                "priority": "40000",
                "in_port": str(input_port),
                "eth_src": src_mac,
                "eth_dst": dst_mac,
                "eth_type": "0x0800",
                "ipv4_src": src_ip,
                "ipv4_dst": dst_ip,
                "ip_proto": ip_protocol,
                port_field: str(service.port),
                "active": "true",
                "actions": f"output={output_port}"
            }

            arp_name = (
                f"{handler}-{direction}-arp-{flow_number}"
            )

            arp_flow = {
                "switch": switch,
                "name": arp_name,
                "cookie": "0",
                "priority": "39000",
                "in_port": str(input_port),
                "eth_type": "0x0806",
                "arp_spa": src_ip,
                "arp_tpa": dst_ip,
                "active": "true",
                "actions": f"output={output_port}"
            }

            flows.append(ip_flow)
            flows.append(arp_flow)

    create_direction(
        forward_route,
        "forward",
        student.mac,
        server_mac,
        student_ip,
        server.ip,
        forward_port_field
    )

    create_direction(
        reverse_route,
        "reverse",
        server_mac,
        student.mac,
        server.ip,
        student_ip,
        reverse_port_field
    )

    await asyncio.gather(
        *[
            send_flow(controller_ip, flow)
            for flow in flows
        ]
    )

    return [flow["name"] for flow in flows]

def students_menu(students, courses):
    while True:
        print("\n--- Gestión de alumnos ---")
        print("1) Listar todos")
        print("2) Listar por curso")
        print("3) Crear alumno")
        print("4) Volver")

        option = input(">>> ").strip()

        if option == "1":
            print("\nAlumnos registrados:")

            for index, student in enumerate(students, start=1):
                print(
                    f"{index}. {student.name} "
                    f"- MAC: {student.mac}"
                )

        elif option == "2":
            code = input("Código del curso: ").strip().upper()

            course = next(
                (
                    item
                    for item in courses
                    if item.code.upper() == code
                ),
                None
            )

            if course is None:
                print("Curso no encontrado.")
                continue

            print(f"\nAlumnos del curso {course.code}:")

            for student_name in course.students:
                student = next(
                    (
                        item
                        for item in students
                        if item.name == student_name
                    ),
                    None
                )

                if student is not None:
                    print(
                        f"- {student.name} "
                        f"- MAC: {student.mac}"
                    )

        elif option == "3":
            name = input("Nombre del alumno: ").strip()
            mac = input("Dirección MAC: ").strip().lower()

            if any(
                student.name.lower() == name.lower()
                for student in students
            ):
                print("Ya existe un alumno con ese nombre.")

            elif any(
                student.mac.lower() == mac
                for student in students
            ):
                print("Ya existe un alumno con esa MAC.")

            else:
                students.append(Student(name, mac))
                print(f"Alumno {name} creado correctamente.")

        elif option == "4":
            break

        else:
            print("Opción inválida.")


def servers_menu(servers):
    while True:
        print("\n--- Gestión de servidores ---")
        print("1) Listar servidores")
        print("2) Mostrar detalles")
        print("3) Volver")

        option = input(">>> ").strip()

        if option == "1":
            print("\nServidores registrados:")

            for server in servers:
                print(f"- {server.name} - IP: {server.ip}")

        elif option == "2":
            name = input("Nombre del servidor: ").strip()

            server = next(
                (
                    item
                    for item in servers
                    if item.name.lower() == name.lower()
                ),
                None
            )

            if server is None:
                print("Servidor no encontrado.")
                continue

            print(f"\nNombre: {server.name}")
            print(f"IP: {server.ip}")
            print("Servicios:")

            for service in server.services:
                print(f"- {service.name}: "
                    f"{service.protocol}/{service.port}")

        elif option == "3":
            break

        else:
            print("Opción inválida.")


def courses_menu(courses, students):
    while True:
        print("\n--- Gestión de cursos ---")
        print("1) Listar cursos")
        print("2) Buscar por servidor y servicio")
        print("3) Gestionar alumnos")
        print("4) Volver")

        option = input(">>> ").strip()

        if option == "1":
            print("\nCursos registrados:")

            for course in courses:
                print(
                    f"- {course.code}: {course.name} "
                    f"- Estado: {course.state}"
                )

        elif option == "2":
            server_name = input(
                "Nombre del servidor: "
            ).strip()

            service_name = input(
                "Nombre del servicio: "
            ).strip().lower()

            matches = []

            for course in courses:
                for server_policy in course.servers:
                    same_server = (
                        server_policy["name"].lower()
                        == server_name.lower()
                    )

                    allowed_services = [
                        service.lower()
                        for service
                        in server_policy["allowed_services"]
                    ]

                    if (
                        same_server
                        and service_name in allowed_services
                    ):
                        matches.append(course)

            if not matches:
                print("No se encontraron cursos.")
                continue

            print(
                f"\nCursos con acceso a {service_name} "
                f"en {server_name}:"
            )

            for course in matches:
                print(
                    f"- {course.code}: {course.name} "
                    f"- Estado: {course.state}"
                )

        elif option == "3":
            code = input(
                "Código del curso: "
            ).strip().upper()

            student_name = input(
                "Nombre del alumno: "
            ).strip()

            course = next(
                (
                    item
                    for item in courses
                    if item.code.upper() == code
                ),
                None
            )

            student = next(
                (
                    item
                    for item in students
                    if item.name.lower()
                    == student_name.lower()
                ),
                None
            )

            if course is None:
                print("Curso no encontrado.")
                continue

            if student is None:
                print("Alumno no encontrado.")
                continue

            print("1) Agregar alumno")
            print("2) Eliminar alumno")

            action = input(">>> ").strip()

            if action == "1":
                if student.name in course.students:
                    print(
                        "El alumno ya pertenece al curso."
                    )
                else:
                    course.students.append(student.name)
                    print(
                        f"{student.name} fue agregado "
                        f"al curso {course.code}."
                    )

            elif action == "2":
                if student.name not in course.students:
                    print(
                        "El alumno no pertenece al curso."
                    )
                else:
                    course.students.remove(student.name)
                    print(
                        f"{student.name} fue eliminado "
                        f"del curso {course.code}."
                    )

            else:
                print("Opción inválida.")
                continue

            print(
                f"\nAlumnos actuales de {course.code}:"
            )

            for name in course.students:
                print(f"- {name}")

        elif option == "4":
            break

        else:
            print("Opción inválida.")

def is_authorized(student, server, service, courses):
    for course in courses:
        if course.state.upper() != "ACTIVE":
            continue

        if student.name not in course.students:
            continue

        for server_policy in course.servers:
            same_server = (
                server_policy["name"].lower()
                == server.name.lower()
            )

            allowed_services = [
                name.lower()
                for name
                in server_policy["allowed_services"]
            ]

            if (
                same_server
                and service.name.lower()
                in allowed_services
            ):
                return True

    return False

async def remove_route(controller_ip, flow_names):
    await asyncio.gather(
        *[
            remove_flow(controller_ip, name)
            for name in flow_names
        ]
    )

def connections_menu(
    students,
    servers,
    courses,
    connections
):
    controller_ip = "127.0.0.1"

    while True:
        print("\n--- Gestión de conexiones ---")
        print("1) Crear conexión")
        print("2) Listar conexiones")
        print("3) Eliminar conexión")
        print("4) Volver")

        option = input(">>> ").strip()

        if option == "1":
            student_name = input(
                "Nombre del alumno: "
            ).strip()

            server_name = input(
                "Nombre del servidor: "
            ).strip()

            service_name = input(
                "Nombre del servicio: "
            ).strip()

            student = next(
                (
                    item
                    for item in students
                    if item.name.lower()
                    == student_name.lower()
                ),
                None
            )

            server = next(
                (
                    item
                    for item in servers
                    if item.name.lower()
                    == server_name.lower()
                ),
                None
            )

            if student is None:
                print("Alumno no encontrado.")
                continue

            if server is None:
                print("Servidor no encontrado.")
                continue

            service = next(
                (
                    item
                    for item in server.services
                    if item.name.lower()
                    == service_name.lower()
                ),
                None
            )

            if service is None:
                print("Servicio no encontrado.")
                continue

            if not is_authorized(
                student,
                server,
                service,
                courses
            ):
                print(
                    "Error: el alumno no está autorizado "
                    "para acceder al servicio."
                )
                continue

            number = 1

            while any(
                connection["handler"]
                == f"connection-{number}"
                for connection in connections
            ):
                number += 1

            handler = f"connection-{number}"

            try:
                flow_names = asyncio.run(
                    build_route(
                        controller_ip,
                        student,
                        server,
                        service,
                        handler
                    )
                )

            except Exception as error:
                print(f"Error al crear la conexión: {error}")
                continue

            connections.append(
                {
                    "handler": handler,
                    "student": student,
                    "server": server,
                    "service": service,
                    "flow_names": flow_names
                }
            )

            print(
                f"Conexión creada correctamente. "
                f"Handler: {handler}"
            )

        elif option == "2":
            if not connections:
                print("No existen conexiones.")
                continue

            print("\nConexiones creadas:")

            for connection in connections:
                print(
                    f"- {connection['handler']}: "
                    f"{connection['student'].name} -> "
                    f"{connection['server'].name} / "
                    f"{connection['service'].name}"
                )
        elif option == "3":
            handler = input(
                "Handler de la conexión: "
            ).strip()

            connection = next(
                (
                    item
                    for item in connections
                    if item["handler"] == handler
                ),
                None
            )

            if connection is None:
                print("Conexión no encontrada.")
                continue

            student = connection["student"]
            server = connection["server"]
            service = connection["service"]

            if not is_authorized(
                student,
                server,
                service,
                courses
            ):
                print(
                    "Error: el alumno ya no está "
                    "autorizado para esta conexión."
                )
                continue

            try:
                asyncio.run(
                    remove_route(
                        controller_ip,
                        connection["flow_names"]
                    )
                )

            except Exception as error:
                print(
                    f"Error al eliminar la conexión: "
                    f"{error}"
                )
                continue

            connections.remove(connection)

            print(
                f"Conexión {handler} "
                "eliminada correctamente."
            )

        elif option == "4":
            break

        else:
            print("Opción inválida.")


def main():
    students, servers, courses = load_data(
        "database.yaml"
    )

    connections = []

    print(f"Alumnos cargados: {len(students)}")
    print(f"Servidores cargados: {len(servers)}")
    print(f"Cursos cargados: {len(courses)}")

    while True:
        option = menu()

        if option == "3":
            courses_menu(courses, students)

        elif option == "4":
            students_menu(students, courses)

        elif option == "5":
            servers_menu(servers)

        elif option == "7":
            connections_menu(
                students,
                servers,
                courses,
                connections
            )

        elif option == "8":
            print("Programa finalizado.")
            break

        else:
            print("Opción todavía no implementada.")	    

if __name__ == "__main__":
    main()
