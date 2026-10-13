#!/usr/bin/env bash
# =============================================================================
# Health Sync - Configuración base de la Raspberry Pi 3B+
# Sistema: Raspberry Pi OS Lite (64 bits), sin escritorio.
#
# Uso:
#   sudo bash setup_rpi.sh
#
# Opciones (variables antes del comando):
#   PRINTER_IP=192.168.1.50 sudo -E bash setup_rpi.sh   -> además añade la impresora
#   INSTALL_DOCKER=1        sudo -E bash setup_rpi.sh   -> además instala Docker (IPTV, web)
#
# Se puede ejecutar varias veces sin romper nada.
# =============================================================================
set -euo pipefail

APP_USER="healthsync"
APP_DIR="/opt/healthsync"
DATA_DIR="/var/lib/healthsync"
LOG_FILE="/var/log/healthsync-setup.log"

paso() { echo -e "\n\033[1;34m==> $*\033[0m"; }
ok()   { echo -e "\033[1;32m    ✓ $*\033[0m"; }
aviso(){ echo -e "\033[1;33m    ! $*\033[0m"; }

exec > >(tee -a "$LOG_FILE") 2>&1

# --- 0. Comprobaciones ------------------------------------------------------
paso "Comprobaciones previas"
if [[ $EUID -ne 0 ]]; then echo "Ejecuta con sudo: sudo bash $0"; exit 1; fi
ARCH=$(uname -m)
if [[ "$ARCH" != "aarch64" ]]; then
  echo "El sistema es $ARCH. Hace falta Raspberry Pi OS de 64 bits (aarch64). Vuelve a grabar la tarjeta."; exit 1
fi
REAL_USER="${SUDO_USER:-pi}"
ok "Sistema de 64 bits; usuario principal: $REAL_USER"

# --- 1. Zona horaria y actualizaciones -----------------------------------
paso "Zona horaria y actualización del sistema (puede tardar 10-20 minutos)"
timedatectl set-timezone Europe/Madrid
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get -o Dpkg::Options::="--force-confold" upgrade -y
ok "Sistema actualizado, hora de Madrid"

# --- 2. Paquetes necesarios -------------------------------------------------
paso "Instalando paquetes del proyecto"
apt-get install -y \
  python3 python3-venv python3-pip python3-dev \
  git sqlite3 curl ca-certificates jq \
  cups cups-client cups-bsd avahi-daemon \
  age rclone \
  unattended-upgrades apt-listchanges \
  ufw
ok "Paquetes instalados"

# --- 3. Actualizaciones de seguridad automáticas ---------------------------
paso "Actualizaciones de seguridad automáticas"
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
EOF
ok "Activadas"

# --- 4. Memoria: zram en lugar de swap en la tarjeta -----------------------
paso "Memoria comprimida (zram) y sin swap en la microSD"
if swapon --show=NAME --noheadings | grep -q zram; then
  # Las versiones recientes de Raspberry Pi OS ya traen zram: no se duplica
  ok "El sistema ya usa zram ($(swapon --show=NAME,SIZE --noheadings | grep zram | tr -s ' ')); se mantiene"
else
  apt-get install -y zram-tools
  cat > /etc/default/zramswap <<'EOF'
ALGO=zstd
PERCENT=50
PRIORITY=100
EOF
  systemctl enable --now zramswap.service || aviso "zramswap no arrancó; revisa con: systemctl status zramswap"
  if systemctl list-unit-files | grep -q '^dphys-swapfile'; then
    dphys-swapfile swapoff || true
    systemctl disable --now dphys-swapfile || true
    ok "Swap en la tarjeta desactivado"
  fi
  sysctl -w vm.swappiness=100 >/dev/null
  echo "vm.swappiness=100" > /etc/sysctl.d/99-zram.conf
  ok "zram activo"
fi

# --- 5. Menos escrituras en la microSD -------------------------------------
paso "Reduciendo escrituras en la microSD"
mkdir -p /etc/systemd/journald.conf.d
cat > /etc/systemd/journald.conf.d/healthsync.conf <<'EOF'
[Journal]
Storage=persistent
SystemMaxUse=60M
SystemMaxFileSize=10M
MaxRetentionSec=1month
EOF
systemctl restart systemd-journald
# noatime en la partición raíz: no escribir la fecha de acceso cada vez que se lee un fichero
if ! grep -E '\s/\s' /etc/fstab | grep -q noatime; then
  cp /etc/fstab /etc/fstab.bak
  sed -i -E 's#(\s/\s+ext4\s+)defaults#\1defaults,noatime#' /etc/fstab
  ok "noatime añadido (copia en /etc/fstab.bak; se aplica al reiniciar)"
fi
ok "Logs limitados a 60 MB"

# --- 6. Cortafuegos ---------------------------------------------------------
paso "Cortafuegos (solo SSH entrante)"
ufw allow OpenSSH >/dev/null
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw --force enable >/dev/null
ok "ufw activo: entrada solo por SSH"

# --- 7. Usuario y carpetas del proyecto ------------------------------------
paso "Usuario y carpetas de Health Sync"
if ! id "$APP_USER" &>/dev/null; then
  useradd --system --create-home --home-dir "$DATA_DIR" --shell /usr/sbin/nologin "$APP_USER"
fi
usermod -aG lpadmin "$APP_USER"
mkdir -p "$APP_DIR" "$DATA_DIR"/{db,secrets,backups,pdf,logs}
chown -R "$APP_USER:$APP_USER" "$APP_DIR" "$DATA_DIR"
chmod 750 "$APP_DIR" "$DATA_DIR"
chmod 700 "$DATA_DIR/secrets"
# El usuario principal puede leer y desplegar código, pero no ve los secretos
usermod -aG "$APP_USER" "$REAL_USER"
ok "Código en $APP_DIR; datos en $DATA_DIR (secretos con permisos 700)"

paso "Entorno de Python del proyecto"
if [[ ! -d "$APP_DIR/venv" ]]; then
  sudo -u "$APP_USER" python3 -m venv "$APP_DIR/venv"
fi
sudo -u "$APP_USER" "$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
sudo -u "$APP_USER" "$APP_DIR/venv/bin/pip" install --quiet \
  requests google-auth google-auth-oauthlib python-dateutil
ok "Python $(python3 --version | cut -d' ' -f2) con entorno propio"

# --- 8. Impresora (opcional ahora) -----------------------------------------
paso "Impresora"
systemctl enable --now cups
if [[ -n "${PRINTER_IP:-}" ]]; then
  lpadmin -p Brother -E -v "ipp://${PRINTER_IP}/ipp/print" -m everywhere
  lpadmin -d Brother
  lpoptions -p Brother -o media=iso_a4_210x297mm -o sides=one-sided
  ok "Impresora Brother añadida en $PRINTER_IP como predeterminada"
  echo "    Prueba: echo 'Hola desde la Raspberry' | lp"
else
  aviso "Sin PRINTER_IP: la impresora se añade después (ver instrucciones)"
fi

# --- 9. Docker (opcional) ---------------------------------------------------
if [[ "${INSTALL_DOCKER:-0}" == "1" ]]; then
  paso "Docker"
  if ! command -v docker &>/dev/null; then
    curl -fsSL https://get.docker.com | sh
  fi
  usermod -aG docker "$REAL_USER"
  ok "Docker instalado (cierra y vuelve a abrir la sesión SSH para usarlo sin sudo)"
fi

# --- 10. Resumen ------------------------------------------------------------
paso "Resumen"
echo "    Modelo:     $(tr -d '\0' < /proc/device-tree/model)"
echo "    Sistema:    $(. /etc/os-release; echo "$PRETTY_NAME") ($(uname -m))"
echo "    Memoria:    $(free -h | awk '/Mem:/ {print $2" total, "$7" disponible"}')"
echo "    Swap zram:  $(swapon --show=NAME,SIZE --noheadings | tr '\n' ' ')"
echo "    Disco:      $(df -h / | awk 'NR==2 {print $4" libres de "$2}')"
echo "    Hora:       $(date '+%d/%m/%Y %H:%M %Z')"
echo
ok "Configuración base terminada. Reinicia con: sudo reboot"
echo "    Registro completo en $LOG_FILE"
