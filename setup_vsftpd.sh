#!/usr/bin/env bash
set -e

echo "=== [1/5] Generating TLS/SSL Certificate ==="
sudo mkdir -p /etc/ssl/private /etc/ssl/certs
sudo openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
  -keyout /etc/ssl/private/vsftpd.pem \
  -out /etc/ssl/certs/vsftpd.pem \
  -subj "/C=US/ST=State/L=City/O=Organization/CN=ftp.local"
sudo chmod 600 /etc/ssl/private/vsftpd.pem
sudo chmod 644 /etc/ssl/certs/vsftpd.pem

echo "=== [2/5] Writing vsftpd.conf ==="
if [ -f /etc/vsftpd/vsftpd.conf ] && [ ! -f /etc/vsftpd/vsftpd.conf.orig ]; then
  sudo cp /etc/vsftpd/vsftpd.conf /etc/vsftpd/vsftpd.conf.orig
fi

sudo tee /etc/vsftpd/vsftpd.conf > /dev/null << 'EOF'
# General / Connection Settings
anonymous_enable=NO
local_enable=YES
write_enable=YES
local_umask=022
dirmessage_enable=YES
xferlog_enable=YES
connect_from_port_20=YES
xferlog_std_format=YES
listen=NO
listen_ipv6=YES
pam_service_name=vsftpd
userlist_enable=YES

# Chroot Jail (Lock users to home directories)
chroot_local_user=YES
allow_writeable_chroot=YES

# Passive Mode Ports
pasv_enable=YES
pasv_min_port=30000
pasv_max_port=31000

# SSL/TLS (FTPS)
ssl_enable=YES
allow_anon_ssl=NO
force_local_data_ssl=YES
force_local_logins_ssl=YES
ssl_tlsv1_2=YES
ssl_tlsv1_3=YES
rsa_cert_file=/etc/ssl/certs/vsftpd.pem
rsa_private_key_file=/etc/ssl/private/vsftpd.pem
require_ssl_reuse=NO
ssl_ciphers=HIGH
EOF

echo "=== [3/5] Setting password for user divyansh ==="
echo "divyansh:divyansh" | sudo chpasswd

echo "=== [4/5] Configuring Firewall & SELinux ==="
if command -v firewall-cmd &>/dev/null && sudo firewall-cmd --state &>/dev/null; then
  sudo firewall-cmd --permanent --add-service=ftp || true
  sudo firewall-cmd --permanent --add-port=30000-31000/tcp || true
  sudo firewall-cmd --reload || true
fi

if command -v setsebool &>/dev/null; then
  sudo setsebool -P ftpd_full_access 1 || true
fi

echo "=== [5/5] Enabling and Starting vsftpd ==="
sudo systemctl enable --now vsftpd
sudo systemctl restart vsftpd

echo "=== Done! vsftpd status: ==="
sudo systemctl status vsftpd --no-pager
