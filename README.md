# Кластер ПАРКС через Google Compute Engine API

Завдання 2 з курсу «Хмарні обчислення»: PoC-інтеграція з хмарним API.
API: **Google Compute Engine API** (v1), клієнт — Python-бібліотека `google-cloud-compute`.

У ЛР1 ([parcs-factorization](https://github.com/yuliiayaresko/parcs-factorization)) кластер
ПАРКС для факторизації числа розгортав bash-скрипт: `gcloud` створював машини, а далі все
налаштовувалося через `ssh` — із повторами, бо свіжа машина довго не приймає з'єднання, і з
пасткою `pkill -f`, яка вбивала власну ssh-сесію.

Тут те саме робить одна Python-програма, яка звертається до Compute Engine API напряму і
**жодного разу не заходить на машини по ssh**. Машини налаштовують себе самі, а з
оркестратором спілкуються через сервер метаданих.

```
python3 parcs_cloud.py run
```

створює кластер, чекає, доки він підніметься, запускає факторизацію з ЛР1, забирає
результати й видаляє машини — також і тоді, коли щось пішло не так.

## Що таке Compute Engine API

REST API для керування віртуальними машинами Google Cloud. Усе, що робить консоль і
`gcloud compute`, — це виклики саме цього API. Основні ресурси:

| ресурс | що це | приклади методів |
|---|---|---|
| `instances` | віртуальні машини | `insert`, `get`, `list`, `delete`, `start`, `stop`, `setMetadata` |
| `machineTypes` | конфігурації CPU/RAM | `get`, `list` |
| `images` | образи дисків ОС | `getFromFamily` |
| `firewalls` | правила брандмауера VPC | `insert`, `get` |
| `regions`, `zones` | розташування, квоти | `get` |
| `zoneOperations` | довгі операції | `get`, `wait` |

Ключова особливість: усі методи, що **змінюють** ресурси, повертають не результат, а
**довгу операцію** (long-running operation). Запит `insert` відповідає за ~0,5 с, а машина
створюється у фоні ще кілька секунд. Тому можна відправити запити на всі машини одразу і
лише потім чекати операції — створення відбувається паралельно.

Автентифікація — через Application Default Credentials: у Cloud Shell вони вже є, локально
їх дає `gcloud auth application-default login`.

### Сфери застосування

- інфраструктура як код: Terraform, Pulumi, Deployment Manager працюють поверх цього API;
- автомасштабування власними правилами: створювати воркери під чергу задач і видаляти після;
- CI/CD: тимчасові тестові середовища на кожен pull request;
- наукові та пакетні обчислення: підняти кластер на час розрахунку, як у цьому проєкті;
- контроль витрат: знаходити й зупиняти машини за мітками, розкладом чи завантаженням.

## Як працює PoC

```mermaid
sequenceDiagram
    participant O as parcs_cloud.py
    participant API as Compute Engine API
    participant D as демони
    participant H as hosts-server
    participant A as app
    O->>API: regions.get, machineTypes.get (квота)
    O->>API: firewalls.insert
    O->>API: instances.insert × N (паралельно)
    API-->>D: створення, startup-script
    API-->>H: створення, startup-script
    API-->>A: створення, startup-script
    D->>D: Java, Daemon.jar
    D-->>O: guest attribute status=ready
    O->>API: setMetadata(hosts-server, parcs-daemons)
    H->>H: hosts.list, TCPHostsServer.jar
    H-->>O: status=ready
    O->>API: setMetadata(app, parcs-server)
    A->>H: адреса вільного демона
    A->>D: точки ПАРКС, відрізки перебору
    O->>API: getSerialPortOutput (живий лог)
    A-->>O: status=done, results.csv
    O->>API: instances.list + delete
```

| крок | виклик API | навіщо |
|---|---|---|
| 1 | `regions.get`, `machineTypes.get` | прочитати квоту CPUS **до** створення і підібрати кількість демонів |
| 2 | `firewalls.get` / `firewalls.insert` | TCP лише між машинами з тегом `parcs` |
| 3 | `instances.insert` × N | усі машини паралельно; startup-script і параметри — у метаданих |
| 4 | `instances.getGuestAttributes` | машина сама звітує: `installing` → `waiting` → `ready` |
| 5 | `instances.get` | внутрішні IP демонів |
| 6 | `instances.setMetadata` | передати hosts-server адреси демонів, коли ті готові |
| 7 | `instances.setMetadata` | передати застосунку адресу hosts-server |
| 8 | `instances.getSerialPortOutput` | живий лог обчислень без ssh |
| 9 | `instances.getGuestAttributes` | забрати `results.csv` і лог |
| 10 | `instances.list` + `instances.delete` | видалити все з міткою `lab=parcs-api` |

### Метадані та guest attributes замість ssh

Кожна машина має локальний сервер метаданих `metadata.google.internal`. Через нього йде
зв'язок в обидва боки:

- **оркестратор → машина**: ключі метаданих (`startup-script`, `parcs-input`,
  `parcs-daemons`, `parcs-server`). Машина читає їх `curl`-ом, а коли ключа ще немає —
  чекає з `wait_for_change=true`: з'єднання висить, доки оркестратор не викличе `setMetadata`;
- **машина → оркестратор**: guest attributes. Машина пише `PUT .../guest-attributes/parcs/status`,
  оркестратор читає `getGuestAttributes`. Так передаються статус, помилка і результати.

Порядок запуску ПАРКС (спершу демони, потім hosts-server зі списком їхніх адрес, потім
застосунок) забезпечується тим, **коли** оркестратор дописує метадані. Машини створюються
одночасно і, поки встановлюють Java, нікого не чекають.

`setMetadata` замінює весь набір ключів і вимагає `fingerprint` поточної версії —
це оптимістичне блокування: якщо метадані змінилися між `get` і `setMetadata`, запит
відхиляється, а не перезаписує чужі зміни.

### Безпека й вартість

- Сервісного акаунта на машинах немає: їм не потрібно звертатися до API Google.
- Брандмауер відкриває TCP лише між машинами з тегом `parcs`, а не `0.0.0.0/0`, як
  `allow-all` у методичці. Портів 2222/3333 недостатньо: канали ПАРКС відкриваються на
  довільних портах.
- Усі машини мають мітку `lab=parcs-api`. Видалення стоїть у `finally`, тому спрацьовує і при
  помилці, і при Ctrl+C. Якщо сам оркестратор було вбито, `python3 parcs_cloud.py delete`
  знайде машини за міткою.
- Типи машин — як у ЛР1: `e2-standard-2` з виділеними ядрами для app і демонів, бо на
  burstable-машинах час міряти не можна; `e2-micro` для hosts-server.

## Структура

```
parcs_cloud.py          оркестратор: усі виклики API, команди run / list / delete
startup/common.sh       спільна частина startup-script: читання метаданих, звіти, очікування
startup/daemon.sh       демон ПАРКС
startup/hosts-server.sh hosts-server ПАРКС
startup/app.sh          застосунок: клонує ЛР1, отримує адресу, запускає make
results/                результати запуску (створюються програмою)
```

## Запуск

У [Cloud Shell](https://shell.cloud.google.com) проєкт і облікові дані вже налаштовані:

```
git clone https://github.com/yuliiayaresko/parcs-compute-api.git
cd parcs-compute-api
pip install -r requirements.txt
gcloud services enable compute.googleapis.com
python3 parcs_cloud.py run
```

Параметри:

```
python3 parcs_cloud.py run --daemons 2 --workers 1 2 4 --repeats 1   # швидкий і дешевий прогін
python3 parcs_cloud.py run --keep          # не видаляти машини після запуску
python3 parcs_cloud.py run --plan          # лише показати JSON запитів, нічого не створюючи
python3 parcs_cloud.py --zone europe-west1-b run
python3 parcs_cloud.py list                # машини з міткою lab=parcs-api
python3 parcs_cloud.py delete              # видалити їх
```

Програма пише в `results/`:

- `results.csv` — таблиця факторизації (час, прискорення, ефективність) з машини app;
- `run.log` — повний вивід застосунку ПАРКС;
- `timeline.csv` — коли кожна машина пройшла кожен етап;
- `api_calls.csv` — скільки разів викликано кожен метод API і з якою затримкою.

## Результати

Заповнюються після хмарного прогону.
