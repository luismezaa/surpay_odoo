FROM odoo:18.0

USER root

# Parchear paquetes Debian npm con CVEs críticos heredados de la imagen base oficial:
# - handlebars <4.7.9: CVE-2026-33937 (CVSS 9.8 CRITICAL), CVE-2026-33938/39/40/41 (HIGH)
# - @babel/traverse <7.23.2: CVE-2023-45133 (CRITICAL)
# Instalados por Odoo via apt como dependencias de nodejs-less en /usr/share/nodejs/.
# Se sobreescriben los archivos del paquete con versiones parcheadas descargadas via npm.
RUN npm install --prefix /tmp/npm-patch handlebars@^4.7.9 @babel/traverse@^7.23.2 2>/dev/null \
    && cp -rf /tmp/npm-patch/node_modules/handlebars/. /usr/share/nodejs/handlebars/ \
    && cp -rf /tmp/npm-patch/node_modules/@babel/traverse/. /usr/share/nodejs/@babel/traverse/ \
    && rm -rf /tmp/npm-patch ~/.npm

COPY docker/requirements.txt /tmp/requirements.txt
RUN if [ -s /tmp/requirements.txt ]; then \
      apt-get update \
      && apt-get install -y --no-install-recommends \
          build-essential \
          gcc \
          python3-dev \
          libldap2-dev \
          libsasl2-dev \
          libxml2-dev \
          libxslt1-dev \
          zlib1g-dev \
          libjpeg-dev \
      && pip3 install --no-cache-dir --break-system-packages -r /tmp/requirements.txt \
      && apt-get purge -y --auto-remove \
          build-essential \
          gcc \
          python3-dev \
          libldap2-dev \
          libsasl2-dev \
          libxml2-dev \
          libxslt1-dev \
          zlib1g-dev \
          libjpeg-dev \
      && rm -rf /var/lib/apt/lists/*; \
    fi

USER odoo
