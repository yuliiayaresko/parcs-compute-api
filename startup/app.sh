# Застосунок ПАРКС: код факторизації з ЛР1, кореневий модуль і послідовний еталон.

report status installing
echo "встановлення $JDK, git, make"
install "$JDK" git make || fail "не вдалося встановити пакети"

repo=$(attr parcs-repo) || fail "у метаданих немає parcs-repo"
rm -rf lab
git clone -q "$repo" lab || fail "не вдалося клонувати $repo"
cd lab || exit 1

# Вхідні дані приходять з оркестратора, тому кількість воркерів і повторів
# змінюється параметрами командного рядка, без нового коміту в репозиторій ЛР1.
attr parcs-input > out/input || fail "у метаданих немає parcs-input"
echo "вхідні дані:"
cat out/input

report status waiting
echo "чекаю адресу hosts-server у метаданих"
wait_attr parcs-server > out/server
echo "hosts-server: $(cat out/server)"

report status running
make 2>&1 | tee run.log
code=${PIPESTATUS[0]}

report_file log run.log
[ "$code" -eq 0 ] || fail "make завершився з кодом $code"
[ -f out/results.csv ] || fail "програма не створила results.csv"
report_file results out/results.csv
report status done
