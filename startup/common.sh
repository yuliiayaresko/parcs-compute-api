#!/bin/bash
# Спільна частина startup-script для всіх ролей. Оркестратор склеює цей файл
# зі скриптом ролі й кладе результат у метадані машини під ключем startup-script.
#
# Зв'язок з оркестратором іде через сервер метаданих, без ssh:
#   читати  — instance/attributes/КЛЮЧ       (оркестратор пише через setMetadata)
#   писати  — instance/guest-attributes/parcs/КЛЮЧ  (оркестратор читає через getGuestAttributes)

set -u
export DEBIAN_FRONTEND=noninteractive

# Увесь вивід скрипта дублюється в послідовний порт із міткою PARCS| — звідти
# оркестратор показує живий лог через getSerialPortOutput. Покладатися на те, що
# агент гостьової ОС сам перешле туди вивід startup-script, не можна: у образі
# Debian 12 цього не сталося, і лог у першому прогоні був порожній.
exec > >(tee >(sed -u 's/^/PARCS| /' > /dev/ttyS0)) 2>&1

MD=http://metadata.google.internal/computeMetadata/v1/instance
JDK=openjdk-17-jdk-headless
WORKDIR=/opt/parcs

attr() {
  curl -sf -H 'Metadata-Flavor: Google' "$MD/attributes/$1"
}

report() {
  curl -sf -X PUT -H 'Metadata-Flavor: Google' --data-binary "$2" \
    "$MD/guest-attributes/parcs/$1" > /dev/null
}

report_file() {
  # Значення guest attribute обмежене за розміром, тому лог обрізається з початку.
  tail -c 60000 "$2" | curl -sf -X PUT -H 'Metadata-Flavor: Google' --data-binary @- \
    "$MD/guest-attributes/parcs/$1" > /dev/null
}

fail() {
  echo "ПОМИЛКА: $1"
  report error "$1"
  report status failed
  exit 1
}

# Чекає, доки оркестратор допише ключ у метадані. wait_for_change тримає з'єднання,
# доки метадані не зміняться, тому це не опитування кожні кілька секунд.
wait_attr() {
  local value
  until value=$(attr "$1"); do
    curl -sf -H 'Metadata-Flavor: Google' \
      "$MD/attributes/?recursive=true&wait_for_change=true&timeout_sec=60" > /dev/null || sleep 5
  done
  echo "$value"
}

# Під час першого завантаження apt може бути зайнятий автооновленнями, тому
# замість миттєвої помилки чекаємо на блокування до п'яти хвилин.
install() {
  apt-get -o DPkg::Lock::Timeout=300 update -qq &&
    apt-get -o DPkg::Lock::Timeout=300 install -y -qq "$@" > /dev/null
}

wait_port() {
  for _ in $(seq 1 60); do
    ss -ltn | grep -q ":$1 " && return 0
    sleep 1
  done
  return 1
}

mkdir -p "$WORKDIR"
cd "$WORKDIR" || exit 1
