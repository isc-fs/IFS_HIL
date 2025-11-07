can_tx_periodic.py - uso rápido

Script para enviar tramas CAN periódicas (pruebas con vcan).

Caracteristicas:
- Usa `python-can` si está instalado (preferido).
- Si no está, usa sockets raw (socketcan) en Linux.
- Soporta modo `--dry-run` para imprimir el frame sin enviarlo.

Requisitos:
- Linux con soporte socketcan (módulos can, can_raw, vcan, etc.)
- Para pruebas locales: `vcan` virtual bus (no requiere hardware)
- Recomendado: `can-utils` para monitorizar (candump/cansend)

Crear vcan0 (pruebas):

```bash
sudo modprobe vcan
sudo ip link add dev vcan0 type vcan
sudo ip link set up vcan0
```

Monitorizar:

```bash
candump vcan0
```

Ejemplos:

Enviar 5 tramas (100 ms) a `vcan0`:

```bash
/opt/IFS08_HIL/tools/can_tx_periodic.py -i vcan0 -a 0x123 -d 112233 -p 100 -c 5
```

Modo dry-run (no enviar):

```bash
/opt/IFS08_HIL/tools/can_tx_periodic.py -i vcan0 -a 0x123 -d 112233 --dry-run
```

Notas:
- Para enviar por sockets raw se requieren privilegios (sudo) o capacidades (cap_net_raw).
- Para integraciones, instalar `python-can` con `pip install python-can` puede simplificar el uso.
