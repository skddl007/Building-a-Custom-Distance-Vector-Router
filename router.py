import json
import ipaddress
import os
import socket
import subprocess
import threading
import time

# Protocol constants
PORT = 5000
VERSION = 1.0
UPDATE_INTERVAL = 2
ROUTE_TIMEOUT = 12
INFINITY = 16

# Values injected by Docker env vars
MY_IP = os.getenv("MY_IP", "127.0.0.1").strip()
NEIGHBORS = [n.strip() for n in os.getenv("NEIGHBORS", "").split(",") if n.strip()]
DIRECT_SUBNETS = [
    s.strip() for s in os.getenv("DIRECT_SUBNETS", "").split(",") if s.strip()
]

# Each route looks like:
# {
#   "10.0.2.0/24": {
#       "distance": 1,
#       "next_hop": "10.0.1.2",
#       "learned_from": "10.0.1.2" or "self",
#       "last_updated": unix_timestamp
#   }
# }
routing_table = {}
table_lock = threading.Lock()


def now():
    return time.time()


def make_route(dist, hop, source, ts):
    return {
        "distance": dist,
        "next_hop": hop,
        "learned_from": source,
        "last_updated": ts,
    }


def parse_packet(raw_data):
    try:
        packt = json.loads(raw_data.decode("utf-8"))
    except Exception:
        return None

    if not isinstance(packt, dict):
        return None
    if packt.get("version") != VERSION:
        return None
    if not isinstance(packt.get("routes"), list):
        return None
    return packt


def parse_route_item(rout):
    subnt = rout.get("subnet")
    if not isinstance(subnt, str):
        return None, None

    try:
        neighbr_dist = int(rout.get("distance"))
    except (TypeError, ValueError):
        return None, None

    return subnt, neighbr_dist


def distance_via_neighbor(neighbr_dist):
    if neighbr_dist >= INFINITY:
        return INFINITY
    return min(INFINITY, neighbr_dist + 1)


def set_route_from_neighbor(old_rout, neighbr_ip, new_dist, ts):
    old_rout["distance"] = new_dist
    old_rout["next_hop"] = neighbr_ip if new_dist < INFINITY else "0.0.0.0"
    old_rout["learned_from"] = neighbr_ip
    old_rout["last_updated"] = ts


def set_same_neighbor_route(old_rout, neighbr_ip, new_dist, ts):
    changed = False
    if int(old_rout["distance"]) != new_dist:
        old_rout["distance"] = new_dist
        old_rout["next_hop"] = neighbr_ip if new_dist < INFINITY else "0.0.0.0"
        changed = True
    old_rout["last_updated"] = ts
    return changed


def apply_kernel_route(subnt, entry):
    distnce = int(entry["distance"])
    lernd_from = str(entry["learned_from"])
    nxt_hop = str(entry["next_hop"])

    if lernd_from == "self":
        iface_map = discover_direct_subnet_ifaces()
        iface = iface_map.get(subnt)
        if iface:
            os.system("ip route replace " + subnt + " dev " + iface)
        else:
            os.system("ip route del " + subnt + " >/dev/null 2>&1")
    elif distnce >= INFINITY:
        os.system("ip route del " + subnt + " >/dev/null 2>&1")
    else:
        os.system("ip route replace " + subnt + " via " + nxt_hop)


def discover_direct_subnet_ifaces():
    subnet_to_iface = {}
    try:
        addr_res = subprocess.run(
            ["ip", "-4", "-o", "addr", "show"],
            capture_output=True,
            text=True,
            check=False,
        )
        for line in addr_res.stdout.splitlines():
            parts = line.strip().split()
            if len(parts) < 4 or "inet" not in parts:
                continue

            iface = parts[1]
            if iface == "lo":
                continue

            idx = parts.index("inet")
            if idx + 1 >= len(parts):
                continue
            cidr = parts[idx + 1]
            try:
                subnt = str(ipaddress.ip_interface(cidr).network)
            except ValueError:
                continue

            if subnt.startswith("127."):
                continue
            subnet_to_iface[subnt] = iface
    except Exception:
        pass

    return subnet_to_iface


def discover_iface_ipv4_cidrs():
    iface_rows = []
    try:
        addr_res = subprocess.run(
            ["ip", "-4", "-o", "addr", "show"],
            capture_output=True,
            text=True,
            check=False,
        )
        for line in addr_res.stdout.splitlines():
            parts = line.strip().split()
            if len(parts) < 4 or "inet" not in parts:
                continue
            iface = parts[1]
            if iface == "lo":
                continue

            idx = parts.index("inet")
            if idx + 1 >= len(parts):
                continue
            cidr = parts[idx + 1]
            try:
                intf = ipaddress.ip_interface(cidr)
            except ValueError:
                continue

            if intf.ip.is_loopback:
                continue
            iface_rows.append((iface, str(intf.ip), intf.network))
    except Exception:
        pass
    return iface_rows


def local_source_for_neighbor(neighbr_ip):
    try:
        neigh_ip = ipaddress.ip_address(neighbr_ip)
    except ValueError:
        return None

    for _, local_ip, netw in discover_iface_ipv4_cidrs():
        if neigh_ip in netw:
            return local_ip
    return None


def discover_direct_subnets():
    if DIRECT_SUBNETS:
        return list(DIRECT_SUBNETS)

    discovered = []
    seen = set()
    try:
        # Prefer interface-derived subnets; these remain correct even if route
        # entries were temporarily replaced by learned next-hop routes.
        for subnt in discover_direct_subnet_ifaces().keys():
            if subnt not in seen:
                seen.add(subnt)
                discovered.append(subnt)

        res = subprocess.run(
            ["ip", "-4", "route", "show", "scope", "link"],
            capture_output=True,
            text=True,
            check=False,
        )
        for line in res.stdout.splitlines():
            parts = line.strip().split()
            if not parts:
                continue
            subnt = parts[0]
            if "/" not in subnt:
                continue
            if subnt.startswith("127."):
                continue
            if subnt not in seen:
                seen.add(subnt)
                discovered.append(subnt)
    except Exception:
        pass

    return discovered


def init_direct_routes():
    # Seed routes we are directly connected to.
    ts = now()
    direct_subnets = discover_direct_subnets()
    with table_lock:
        for subnt in direct_subnets:
            routing_table[subnt] = make_route(0, "0.0.0.0", "self", ts)
    if direct_subnets:
        print("Direct subnets loaded:", direct_subnets, flush=True)
    else:
        print("No direct subnets set. Using learned routes only.", flush=True)


def refresh_direct_routes():
    # Evaluator can attach extra interfaces after process start.
    # Re-discover direct subnets periodically and promote them to self routes.
    ts = now()
    discovered = discover_direct_subnets()
    changed = False

    if not discovered:
        return False

    with table_lock:
        discovered_set = set(discovered)

        # If a previously direct network disappeared (e.g., link detach),
        # drop the self route so Bellman-Ford can relearn alternate paths.
        for subnt, old_rout in list(routing_table.items()):
            if old_rout["learned_from"] == "self" and subnt not in discovered_set:
                del routing_table[subnt]
                changed = True

        for subnt in discovered:
            old_rout = routing_table.get(subnt)
            if old_rout is None:
                routing_table[subnt] = make_route(0, "0.0.0.0", "self", ts)
                changed = True
                continue

            if (
                old_rout["learned_from"] != "self"
                or int(old_rout["distance"]) != 0
                or str(old_rout["next_hop"]) != "0.0.0.0"
            ):
                old_rout["distance"] = 0
                old_rout["next_hop"] = "0.0.0.0"
                old_rout["learned_from"] = "self"
                old_rout["last_updated"] = ts
                changed = True
            else:
                old_rout["last_updated"] = ts

    return changed


def build_packet_for_neighbor(targt_neighbr):
    # Build one DV packet per neighbor (split horizon rule).
    # If we learned a route from this neighbor, advertise it as unreachable.
    routs = []
    with table_lock:
        for subnt, entry in routing_table.items():
            distnce = int(entry["distance"])
            lernd_from = str(entry["learned_from"])

            adv_distnce = distnce
            if lernd_from == targt_neighbr:
                adv_distnce = INFINITY

            routs.append({"subnet": subnt, "distance": adv_distnce})

    return {"router_id": MY_IP, "version": VERSION, "routes": routs}


def broadcast_updates():
    sockt = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    src_sockets = {}

    while True:
        if refresh_direct_routes():
            apply_routes_to_kernel()

        for neighbr in NEIGHBORS:
            packt = build_packet_for_neighbor(neighbr)
            try:
                raw_pkt = json.dumps(packt).encode("utf-8")
                src_ip = local_source_for_neighbor(neighbr)
                send_sock = sockt
                if src_ip is not None:
                    if src_ip not in src_sockets:
                        bound = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                        bound.bind((src_ip, 0))
                        src_sockets[src_ip] = bound
                    send_sock = src_sockets[src_ip]
                send_sock.sendto(raw_pkt, (neighbr, PORT))
            except Exception as exc:
                print("Could not send update to", neighbr, ":", exc, flush=True)

        expire_stale_routes()
        time.sleep(UPDATE_INTERVAL)


def listen_for_updates():
    sockt = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sockt.bind(("0.0.0.0", PORT))
    print("Listening for updates on UDP/", PORT, sep="", flush=True)

    while True:
        data, addr = sockt.recvfrom(65535)
        neighbr_ip = addr[0]
        packt = parse_packet(data)
        if packt is not None:
            changed = apply_bellman_ford(neighbr_ip, packt["routes"])
            if changed:
                apply_routes_to_kernel()
                print_table()


def apply_bellman_ford(neighbr_ip, routes_from_neighbr):
    # Apply one neighbor update to our local distance-vector table.
    changed = False
    ts = now()

    with table_lock:
        for rout in routes_from_neighbr:
            subnt = rout.get("subnet")
            if not isinstance(subnt, str):
                continue

            try:
                neighbr_dist = int(rout.get("distance"))
            except (TypeError, ValueError):
                continue

            # Our cost is neighbor cost + 1 hop.
            if neighbr_dist >= INFINITY:
                new_dist = INFINITY
            else:
                new_dist = min(INFINITY, neighbr_dist + 1)

            old_rout = routing_table.get(subnt)  # current best route (if any)

            # First time seeing this subnet.
            if old_rout is None:
                nxt_hop = neighbr_ip if new_dist < INFINITY else "0.0.0.0"
                routing_table[subnt] = make_route(new_dist, nxt_hop, neighbr_ip, ts)
                changed = True
            # Never replace directly connected routes.
            elif old_rout["learned_from"] == "self":
                old_rout["last_updated"] = ts
            # Same source neighbor: refresh or update in place.
            elif str(old_rout["learned_from"]) == neighbr_ip:
                if set_same_neighbor_route(old_rout, neighbr_ip, new_dist, ts):
                    changed = True
            # Different source neighbor: only switch if better.
            else:
                old_dist = int(old_rout["distance"])
                old_hop = str(old_rout["next_hop"])
                if new_dist < old_dist or (new_dist == old_dist and neighbr_ip < old_hop):
                    # Better path found through this neighbor.
                    set_route_from_neighbor(old_rout, neighbr_ip, new_dist, ts)
                    changed = True

    return changed


def expire_stale_routes():
    # If a learned route goes quiet too long, mark it unreachable.
    ts = now()
    changed = False

    with table_lock:
        for subnt, entry in routing_table.items():
            if entry["learned_from"] == "self":
                continue

            age = ts - float(entry["last_updated"])
            if age > ROUTE_TIMEOUT and int(entry["distance"]) < INFINITY:
                entry["distance"] = INFINITY
                entry["next_hop"] = "0.0.0.0"
                changed = True

    if changed:
        print("Some routes timed out and are now unreachable.", flush=True)
        apply_routes_to_kernel()
        print_table()


def apply_routes_to_kernel():
    # Push current best routes into Linux routing table.
    with table_lock:
        for subnt, entry in routing_table.items():
            apply_kernel_route(subnt, entry)


def print_table():
    with table_lock:
        print("\n Routing Table ")
        print("Router:", MY_IP)
        for subnt, entry in sorted(routing_table.items()):
            print(
                subnt,
                "dist:",
                int(entry["distance"]),
                "via:",
                str(entry["next_hop"]),
                "from:",
                str(entry["learned_from"]),
            )


if __name__ == "__main__":
    print("Starting DV router...", flush=True)
    print("My IP:", MY_IP, flush=True)
    print("Neighbors:", NEIGHBORS, flush=True)
    print("Direct subnets:", DIRECT_SUBNETS, flush=True)

    init_direct_routes()
    print_table()

    threading.Thread(target=broadcast_updates, daemon=True).start()
    listen_for_updates()
