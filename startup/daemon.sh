# Демон ПАРКС: за запитом hosts-server запускає точки — окремі процеси JVM з воркером.

report status installing
echo "встановлення $JDK"
install "$JDK" wget || fail "не вдалося встановити $JDK"
wget -q -O Daemon.jar https://github.com/lionell/labs/raw/master/parcs/Daemon/Daemon.jar ||
  fail "не вдалося завантажити Daemon.jar"

# systemd-run робить із демона окрему службу: вона живе далі, коли startup-script
# завершиться, і не залежить від того, як агент гостьової ОС поводиться з дочірніми процесами.
systemd-run --unit=parcs-daemon --working-directory="$WORKDIR" java -jar Daemon.jar ||
  fail "не вдалося запустити Daemon.jar"
wait_port 2222 || fail "демон не відкрив порт 2222"

echo "демон слухає порт 2222"
report status ready
