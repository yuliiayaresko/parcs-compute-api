# Hosts-server ПАРКС: веде список демонів і видає адресу вільної машини.

report status installing
echo "встановлення $JDK"
install "$JDK" wget || fail "не вдалося встановити $JDK"
wget -q -O TCPHostsServer.jar https://github.com/lionell/labs/raw/master/parcs/HostsServer/TCPHostsServer.jar ||
  fail "не вдалося завантажити TCPHostsServer.jar"

# Адреси демонів стають відомі лише після їх створення, тому оркестратор
# дописує їх у метадані цієї машини, коли всі демони вже слухають порт.
report status waiting
echo "чекаю адреси демонів у метаданих"
daemons=$(wait_attr parcs-daemons)
printf '%s\n' $daemons > hosts.list
echo "демони: $daemons"

systemd-run --unit=parcs-hosts --working-directory="$WORKDIR" java -jar TCPHostsServer.jar ||
  fail "не вдалося запустити TCPHostsServer.jar"
wait_port 3333 || fail "hosts-server не відкрив порт 3333"

echo "hosts-server слухає порт 3333"
report status ready
