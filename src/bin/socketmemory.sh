# Runs inside each container as root through docker exec and sh -c.
# Socket tables must be read in each network namespace because host ss alone
# misses sockets on Docker bridge networks.
for file in /proc/net/sockstat /proc/net/sockstat6 /proc/net/tcp /proc/net/tcp6 \
            /proc/net/udp /proc/net/udp6 /sys/fs/cgroup/memory.current \
            /sys/fs/cgroup/memory.stat /sys/fs/cgroup/memory.events; do
    echo "--- $file"
    cat "$file" 2>&1
done
echo '--- socket owners (namespace PIDs and socket inodes)'
if command -v ls >/dev/null && command -v grep >/dev/null; then
    ls -l /proc/[0-9]*/fd/* 2>/dev/null | grep 'socket:'
else
    echo 'unavailable: container lacks ls or grep'
fi
echo '--- processes (namespace PIDs)'
for file in /proc/[0-9]*/comm; do
    echo "$file"
    cat "$file" 2>&1
done
