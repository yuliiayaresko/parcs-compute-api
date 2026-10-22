"""
Розгортання кластера ПАРКС і запуск факторизації через Google Compute Engine API.

Те саме, що в ЛР1 робив bash-скрипт із gcloud і ssh, тут робиться викликами API
з Python-бібліотеки google-cloud-compute:

  1. RegionsClient.get / MachineTypesClient.get — перевірка квоти на vCPU до створення;
  2. FirewallsClient.get / insert            — правило для внутрішнього трафіку ПАРКС;
  3. InstancesClient.insert                  — усі машини створюються паралельно;
  4. метадані + startup-script               — машина налаштовує себе сама, без ssh;
  5. InstancesClient.get_guest_attributes    — машина звітує про свій стан і віддає результати;
  6. InstancesClient.set_metadata            — оркестратор передає адреси, коли вони готові;
  7. InstancesClient.get_serial_port_output  — живий лог застосунку;
  8. InstancesClient.list / delete           — прибирання за міткою, навіть після помилки.

Запуск (Cloud Shell):
  pip install -r requirements.txt
  python3 parcs_cloud.py run
  python3 parcs_cloud.py run --daemons 2 --workers 1 2 4 --keep
  python3 parcs_cloud.py list
  python3 parcs_cloud.py delete
  python3 parcs_cloud.py run --plan     # лише показати ресурси, нічого не створюючи
"""

import argparse
import csv
import json
import os
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from google.api_core import exceptions
from google.cloud import compute_v1

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"
STARTUP = HERE / "startup"

# Усі ресурси лабораторної позначаються міткою: за нею їх знаходить і видаляє
# команда delete, навіть якщо оркестратор упав і не пам'ятає, що створив.
LABEL_KEY, LABEL_VALUE = "lab", "parcs-api"
NETWORK_TAG = "parcs"
FIREWALL_NAME = "parcs-internal"
IMAGE = "projects/debian-cloud/global/images/family/debian-12"
REPO = "https://github.com/yuliiayaresko/parcs-factorization.git"

POLL_SECONDS = 5


@dataclass
class Node:
    name: str
    role: str          # daemon | hosts-server | app
    machine_type: str


class ClusterError(Exception):
    pass


# --------------------------------------------------------------- облік викликів API

class ApiStats:
    """Скільки разів викликано кожен метод API і скільки тривав виклик.

    Для insert/delete/set_metadata це час самого HTTP-запиту, а не операції:
    API відповідає одразу, а машина створюється у фоні (довга операція).
    Час операції рахується окремо — у таймлайні.
    """

    def __init__(self):
        self.calls = defaultdict(list)

    def call(self, method, fn, **kwargs):
        started = time.monotonic()
        try:
            return fn(**kwargs)
        finally:
            self.calls[method].append(time.monotonic() - started)

    def rows(self):
        for method in sorted(self.calls):
            times = self.calls[method]
            yield method, len(times), sum(times) / len(times) * 1000, max(times) * 1000


class Timeline:
    """Моменти подій для кожної машини, у секундах від старту оркестратора."""

    def __init__(self):
        self.start = time.monotonic()
        self.events = defaultdict(dict)

    def now(self):
        return time.monotonic() - self.start

    def mark(self, name, event):
        self.events[name].setdefault(event, self.now())


# ------------------------------------------------------------------- оркестратор

class Cluster:

    def __init__(self, project, zone, plan=False):
        self.project = project
        self.zone = zone
        self.region = zone.rsplit("-", 1)[0]
        self.plan = plan
        self.stats = ApiStats()
        self.timeline = Timeline()
        if not plan:
            self.instances = compute_v1.InstancesClient()
            self.regions = compute_v1.RegionsClient()
            self.machine_types = compute_v1.MachineTypesClient()
            self.firewalls = compute_v1.FirewallsClient()

    # ---------------------------------------------------------------- квота

    def fit_daemons(self, wanted, daemon_type, other_types):
        """Скільки демонів поміщається в регіональну квоту CPUS.

        Квоту треба читати до створення: інакше insert падає з QUOTA_EXCEEDED
        посеред розгортання, а вже створені машини лишаються й тарифікуються.
        """
        if self.plan:
            return wanted

        known = {}

        def cpus(machine_type):
            if machine_type not in known:
                mt = self.stats.call("machineTypes.get", self.machine_types.get,
                                     project=self.project, zone=self.zone, machine_type=machine_type)
                known[machine_type] = mt.guest_cpus
            return known[machine_type]

        region = self.stats.call("regions.get", self.regions.get,
                                 project=self.project, region=self.region)
        quota = next((q for q in region.quotas if q.metric == "CPUS"), None)
        if quota is None:
            print("квоту CPUS прочитати не вдалося, продовжую як є")
            return wanted

        free = int(quota.limit - quota.usage)
        per_daemon = cpus(daemon_type)
        budget = free - sum(cpus(t) for t in other_types)
        fits = max(0, budget // per_daemon)
        print(f"квота CPUS у {self.region}: {int(quota.limit)}, зайнято {int(quota.usage)}, "
              f"вільно {free} → поміщається демонів: {fits}")
        if fits == 0:
            raise ClusterError(
                "квоти не вистачає навіть на одного демона; збільшити її можна тут:\n"
                "  https://console.cloud.google.com/iam-admin/quotas?service=compute.googleapis.com")
        if fits < wanted:
            print(f"увага: замість {wanted} демонів буде {fits}")
        return min(wanted, fits)

    # ----------------------------------------------------------- брандмауер

    def ensure_firewall(self):
        """Дозволяє TCP лише між машинами з тегом parcs.

        Портів 2222 (демон) і 3333 (hosts-server) недостатньо: канали між точками
        ПАРКС відкриваються на довільних портах. Тому відкрито весь TCP, але лише
        всередині кластера, а не з усього інтернету, як allow-all у методичці.
        """
        rule = compute_v1.Firewall(
            name=FIREWALL_NAME,
            network="global/networks/default",
            direction="INGRESS",
            priority=1000,
            allowed=[compute_v1.Allowed(I_p_protocol="tcp")],
            source_tags=[NETWORK_TAG],
            target_tags=[NETWORK_TAG],
            description="PARCS: TCP between cluster nodes only",
        )
        if self.plan:
            show("firewalls.insert", rule)
            return
        try:
            self.stats.call("firewalls.get", self.firewalls.get,
                            project=self.project, firewall=FIREWALL_NAME)
            print(f"правило брандмауера {FIREWALL_NAME} уже є")
        except exceptions.NotFound:
            op = self.stats.call("firewalls.insert", self.firewalls.insert,
                                 project=self.project, firewall_resource=rule)
            op.result(timeout=120)
            print(f"створено правило брандмауера {FIREWALL_NAME}")

    # -------------------------------------------------------------- машини

    def instance_resource(self, node, metadata):
        script = (STARTUP / "common.sh").read_text(encoding="utf-8") + "\n" + \
                 (STARTUP / f"{node.role}.sh").read_text(encoding="utf-8")
        items = {"startup-script": script, "enable-guest-attributes": "TRUE", **metadata}
        return compute_v1.Instance(
            name=node.name,
            machine_type=f"zones/{self.zone}/machineTypes/{node.machine_type}",
            labels={LABEL_KEY: LABEL_VALUE, "role": node.role},
            tags=compute_v1.Tags(items=[NETWORK_TAG]),
            disks=[compute_v1.AttachedDisk(
                boot=True,
                auto_delete=True,
                initialize_params=compute_v1.AttachedDiskInitializeParams(
                    source_image=IMAGE, disk_size_gb=10),
            )],
            # Зовнішня адреса потрібна лише для apt-get і завантаження jar-файлів.
            # Сам ПАРКС спілкується внутрішніми адресами.
            network_interfaces=[compute_v1.NetworkInterface(
                network="global/networks/default",
                access_configs=[compute_v1.AccessConfig(name="External NAT", type_="ONE_TO_ONE_NAT")],
            )],
            metadata=compute_v1.Metadata(
                items=[compute_v1.Items(key=k, value=v) for k, v in items.items()]),
            # Сервісного акаунта немає: машинам не треба звертатися до API Google.
            # Метадані й guest attributes доступні через локальний сервер метаданих без нього.
        )

    def create(self, nodes, metadata_for):
        """Створює всі машини паралельно.

        insert повертає довгу операцію одразу, тому спершу відправляються всі запити,
        і лише потім очікуються операції. Послідовне створення тривало б у рази довше.
        """
        operations = {}
        for node in nodes:
            resource = self.instance_resource(node, metadata_for(node))
            if self.plan:
                show("instances.insert", resource, hide=("startup-script",))
                continue
            self.timeline.mark(node.name, "insert")
            operations[node.name] = self.stats.call(
                "instances.insert", self.instances.insert,
                project=self.project, zone=self.zone, instance_resource=resource)

        for name, op in operations.items():
            op.result(timeout=300)
            self.timeline.mark(name, "created")
            print(f"  {name}: створено за {self.timeline.events[name]['created'] - self.timeline.events[name]['insert']:.1f} с")

    def internal_ip(self, name):
        inst = self.stats.call("instances.get", self.instances.get,
                               project=self.project, zone=self.zone, instance=name)
        return inst.network_interfaces[0].network_i_p

    def set_metadata(self, name, key, value):
        """Дописує ключ у метадані працюючої машини.

        set_metadata замінює весь набір ключів і вимагає fingerprint поточної версії —
        це оптимістичне блокування: якщо метадані встигли змінитися, запит відхиляється.
        """
        if self.plan:
            print(f"[plan] instances.setMetadata {name}: {key}={value!r}")
            return
        inst = self.stats.call("instances.get", self.instances.get,
                               project=self.project, zone=self.zone, instance=name)
        items = {i.key: i.value for i in inst.metadata.items}
        items[key] = value
        metadata = compute_v1.Metadata(
            fingerprint=inst.metadata.fingerprint,
            items=[compute_v1.Items(key=k, value=v) for k, v in items.items()])
        op = self.stats.call("instances.setMetadata", self.instances.set_metadata,
                             project=self.project, zone=self.zone, instance=name,
                             metadata_resource=metadata)
        op.result(timeout=120)

    # ------------------------------------------------------ звіти від машин

    def guest_attributes(self, name):
        """Ключі, які машина записала про себе в простір імен parcs/."""
        # Параметри, яких немає серед іменованих аргументів методу (query_path, filter,
        # port, start), бібліотека приймає лише в об'єкті запиту.
        request = compute_v1.GetGuestAttributesInstanceRequest(
            project=self.project, zone=self.zone, instance=name, query_path="parcs/")
        try:
            attrs = self.stats.call("instances.getGuestAttributes", self.instances.get_guest_attributes,
                                    request=request)
        except exceptions.NotFound:
            return {}  # машина ще нічого не записала
        return {e.key: e.value for e in attrs.query_value.items}

    def wait_for(self, names, final, timeout, on_tick=None):
        """Чекає, доки кожна машина звітує статус final; падає на статусі failed."""
        if self.plan:
            print(f"[plan] чекати status={final} від: {', '.join(names)}")
            return {}
        pending = set(names)
        last = {}
        attributes = {}
        deadline = time.monotonic() + timeout
        while pending:
            for name in sorted(pending):
                attrs = self.guest_attributes(name)
                status = attrs.get("status")
                if status and status != last.get(name):
                    last[name] = status
                    self.timeline.mark(name, status)
                    print(f"  [{self.timeline.now():6.1f} с] {name}: {status}")
                if status == "failed":
                    tail = "\n".join(SerialTail(self, name).read_all()[-20:])
                    raise ClusterError(f"{name}: {attrs.get('error', 'невідома помилка')}\n{tail}")
                if status == final:
                    attributes[name] = attrs
                    pending.discard(name)
            if on_tick:
                on_tick()
            if pending and time.monotonic() > deadline:
                raise ClusterError(f"не дочекалися status={final} від: {', '.join(sorted(pending))}")
            if pending:
                time.sleep(POLL_SECONDS)
        return attributes

    # ----------------------------------------------------------- прибирання

    def list_labeled(self):
        request = compute_v1.ListInstancesRequest(
            project=self.project, zone=self.zone, filter=f"labels.{LABEL_KEY} = {LABEL_VALUE}")
        return list(self.stats.call("instances.list", self.instances.list, request=request))

    def delete_all(self):
        if self.plan:
            print(f"[plan] instances.delete для всіх машин із міткою {LABEL_KEY}={LABEL_VALUE}")
            return
        names = [i.name for i in self.list_labeled()]
        if not names:
            print("машин із міткою немає")
            return
        print(f"видаляю: {', '.join(names)}")
        operations = [self.stats.call("instances.delete", self.instances.delete,
                                      project=self.project, zone=self.zone, instance=name)
                      for name in names]
        for op in operations:
            op.result(timeout=300)
        print("видалено")


class SerialTail:
    """Читає вивід послідовного порту порціями, починаючи з того місця, де зупинився.

    Startup-script дублює свій вивід у порт із міткою PARCS| (startup/common.sh),
    тож звідти видно живий лог без ssh. Решту виводу порту (ядро, systemd) відкидаємо.
    """

    MARKER = "PARCS| "

    def __init__(self, cluster, name):
        self.cluster = cluster
        self.name = name
        self.offset = 0

    def read(self):
        request = compute_v1.GetSerialPortOutputInstanceRequest(
            project=self.cluster.project, zone=self.cluster.zone, instance=self.name,
            port=1, start=self.offset)
        try:
            out = self.cluster.stats.call(
                "instances.getSerialPortOutput", self.cluster.instances.get_serial_port_output,
                request=request)
        except exceptions.GoogleAPICallError:
            return []
        self.offset = out.next_
        lines = []
        for line in out.contents.splitlines():
            if self.MARKER in line:
                lines.append(line.split(self.MARKER, 1)[1].rstrip("\r"))
        return lines

    def read_all(self):
        lines = []
        while True:
            portion = self.read()
            if not portion:
                return lines
            lines.extend(portion)

    def print_new(self):
        for line in self.read():
            print(f"    │ {line}")


def show(method, resource, hide=()):
    data = json.loads(type(resource).to_json(resource))
    for item in data.get("metadata", {}).get("items", []):
        if item["key"] in hide:
            item["value"] = f"<{len(item['value'])} символів>"
    print(f"[plan] {method}")
    print(json.dumps(data, ensure_ascii=False, indent=2))


# ----------------------------------------------------------------- команди

def run(args):
    cluster = Cluster(args.project, args.zone, plan=args.plan)
    say("Квота")
    daemons = cluster.fit_daemons(args.daemons, args.daemon_type, [args.app_type, args.hosts_type])

    nodes = [Node(f"parcs-daemon-{i}", "daemon", args.daemon_type) for i in range(1, daemons + 1)]
    hosts = Node("parcs-hosts", "hosts-server", args.hosts_type)
    app = Node("parcs-app", "app", args.app_type)
    daemon_names = [n.name for n in nodes]

    parcs_input = (f"number={args.number}\n"
                   f"workers={' '.join(str(w) for w in args.workers)}\n"
                   f"repeats={args.repeats}\n")

    def metadata_for(node):
        if node.role == "app":
            return {"parcs-repo": args.repo, "parcs-input": parcs_input}
        return {}

    try:
        say("Брандмауер")
        cluster.ensure_firewall()

        say(f"Створення машин: {daemons} демонів, hosts-server, app")
        cluster.create(nodes + [hosts, app], metadata_for)

        say("Демони встановлюють Java і стартують")
        cluster.wait_for(daemon_names, "ready", timeout=600)

        # Hosts-server чекає ключ parcs-daemons у метаданих: оркестратор дописує його
        # лише тоді, коли всі демони справді слухають порт.
        say("Hosts-server отримує адреси демонів")
        daemon_ips = [] if args.plan else [cluster.internal_ip(n) for n in daemon_names]
        cluster.set_metadata(hosts.name, "parcs-daemons", " ".join(daemon_ips))
        cluster.wait_for([hosts.name], "ready", timeout=600)

        say("Застосунок отримує адресу hosts-server і рахує")
        hosts_ip = "" if args.plan else cluster.internal_ip(hosts.name)
        cluster.set_metadata(app.name, "parcs-server", hosts_ip)
        tail = None if args.plan else SerialTail(cluster, app.name)
        attributes = cluster.wait_for([app.name], "done", timeout=args.timeout,
                                      on_tick=tail.print_new if tail else None)
        if tail:
            tail.print_new()

        if not args.plan:
            save_results(cluster, attributes[app.name], nodes + [hosts, app])
    finally:
        if args.keep:
            say("Машини залишено (--keep). Видалити: python3 parcs_cloud.py delete")
        else:
            say("Видалення машин")
            cluster.delete_all()
        if not args.plan:
            save_api_stats(cluster)


def save_results(cluster, attrs, nodes):
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / "results.csv").write_text(attrs.get("results", ""), encoding="utf-8")
    (RESULTS / "run.log").write_text(attrs.get("log", ""), encoding="utf-8")

    events = ["insert", "created", "installing", "waiting", "ready", "running", "done"]
    with open(RESULTS / "timeline.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["машина", "роль", "тип"] + [f"{e}, с" for e in events])
        for node in nodes:
            marks = cluster.timeline.events.get(node.name, {})
            w.writerow([node.name, node.role, node.machine_type] +
                       [f"{marks[e]:.1f}" if e in marks else "" for e in events])

    say("Результати факторизації")
    print(attrs.get("results", "(порожньо)"))
    print(f"файли збережено в {RESULTS}")


def save_api_stats(cluster):
    rows = list(cluster.stats.rows())
    if not rows:
        return
    RESULTS.mkdir(exist_ok=True)
    with open(RESULTS / "api_calls.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["метод", "викликів", "середня затримка, мс", "максимальна, мс"])
        for method, count, mean, worst in rows:
            w.writerow([method, count, f"{mean:.0f}", f"{worst:.0f}"])

    say("Виклики Compute Engine API")
    print(f"{'метод':34} {'викликів':>9} {'сер., мс':>9} {'макс., мс':>10}")
    for method, count, mean, worst in rows:
        print(f"{method:34} {count:9} {mean:9.0f} {worst:10.0f}")
    print(f"разом: {sum(r[1] for r in rows)} викликів за {cluster.timeline.now():.0f} с")


def list_cmd(args):
    cluster = Cluster(args.project, args.zone)
    instances = cluster.list_labeled()
    if not instances:
        print("машин із міткою немає")
    for inst in instances:
        ip = inst.network_interfaces[0].network_i_p if inst.network_interfaces else ""
        print(f"{inst.name:18} {inst.status:12} {inst.machine_type.rsplit('/', 1)[-1]:15} {ip}")


def delete_cmd(args):
    Cluster(args.project, args.zone).delete_all()


def say(text):
    print(f"\n=== {text} ===")


def default_project():
    project = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("CLOUDSDK_CORE_PROJECT")
    if project:
        return project
    try:
        import google.auth
        return google.auth.default()[1]
    except Exception:
        return None


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="Кластер ПАРКС через Compute Engine API")
    parser.add_argument("--project", default=None, help="ідентифікатор проєкту GCP")
    parser.add_argument("--zone", default="europe-north1-a")
    sub = parser.add_subparsers(dest="command", required=True)

    r = sub.add_parser("run", help="створити кластер, виконати факторизацію, видалити кластер")
    r.add_argument("--daemons", type=int, default=4)
    r.add_argument("--workers", type=int, nargs="+", default=[1, 2, 4, 8])
    r.add_argument("--repeats", type=int, default=3)
    r.add_argument("--number", default="4611685975477714963")
    r.add_argument("--repo", default=REPO, help="репозиторій ЛР1 з кодом факторизації")
    # Типи машин — як у ЛР1: на burstable-машинах (e2-micro/small/medium) час міряти
    # не можна, кредити на CPU вичерпуються за хвилину. Hosts-server лише роздає адреси.
    r.add_argument("--daemon-type", default="e2-standard-2")
    r.add_argument("--app-type", default="e2-standard-2")
    r.add_argument("--hosts-type", default="e2-micro")
    r.add_argument("--timeout", type=int, default=1800, help="скільки чекати на обчислення, с")
    r.add_argument("--keep", action="store_true", help="не видаляти машини після запуску")
    r.add_argument("--plan", action="store_true", help="показати ресурси, нічого не створюючи")
    r.set_defaults(func=run)

    sub.add_parser("list", help="машини з міткою лабораторної").set_defaults(func=list_cmd)
    sub.add_parser("delete", help="видалити всі машини з міткою").set_defaults(func=delete_cmd)

    args = parser.parse_args()
    if not getattr(args, "plan", False):
        args.project = args.project or default_project()
        if not args.project:
            parser.error("не вдалося визначити проєкт: передай --project або виконай "
                         "gcloud config set project ІДЕНТИФІКАТОР")
    else:
        args.project = args.project or "PROJECT_ID"

    try:
        args.func(args)
    except ClusterError as e:
        print(f"\nПОМИЛКА: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nперервано")
        sys.exit(130)


if __name__ == "__main__":
    main()
