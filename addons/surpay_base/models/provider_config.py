import base64
import logging
import requests

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError, UserError
from cryptography.fernet import Fernet

_logger = logging.getLogger(__name__)


class SurpayProviderConfig(models.Model):
    _name = "surpay.provider.config"
    _description = "Configuracion de proveedor de pagos"
    _rec_name = "display_name"

    PROVIDERS = [
        ("depay", "Depay"),
        ("klap", "Klap"),
    ]
    ENVIRONMENTS = [
        ("sandbox", "Sandbox"),
        ("production", "Produccion"),
    ]

    provider = fields.Selection(
        string="Proveedor",
        selection=PROVIDERS,
        required=True,
        index=True,
        help="Proveedor de pago",
    )
    environment = fields.Selection(
        string="Entorno",
        selection=ENVIRONMENTS,
        required=True,
        default="sandbox",
        help="Entorno: Sandbox para pruebas, Produccion para operacion real",
    )
    state = fields.Selection(
        string="Estado",
        selection=[("active", "Activo"), ("inactive", "Inactivo")],
        default="active",
        required=True,
        help="Habilitar o deshabilitar esta configuracion",
    )

    # Encrypted credentials
    api_key = fields.Char(
        required=True,
        help="API Key del proveedor (cifrada en base de datos)",
    )
    customer_uuid = fields.Char(
        required=False,
        help="Customer UUID del proveedor (cifrado en base de datos)",
    )
    pos_id = fields.Char(
        required=False,
        help="ID de punto de venta (cifrado en base de datos)",
    )
    webhook_secret = fields.Char(
        required=False,
        help="Secreto del webhook para validar firma (cifrado en base de datos)",
    )
    base_url = fields.Char(
        required=False,
        help="URL base del API del proveedor (cifrada en base de datos)",
    )

    # Non-encrypted
    webhook_url = fields.Char(
        required=False,
        help="URL webhook para callbacks del proveedor (sin cifrar)",
    )

    # Metadata
    notes = fields.Text(help="Notas internas sobre esta configuracion")
    last_test_date = fields.Datetime(string="Última prueba", readonly=True, help="Ultima prueba de conexion exitosa")
    create_date = fields.Datetime(string="Creado", readonly=True)
    create_uid = fields.Many2one("res.users", string="Creado por", readonly=True)
    write_date = fields.Datetime(string="Modificado", readonly=True)
    write_uid = fields.Many2one("res.users", string="Modificado por", readonly=True)

    @api.depends("provider", "environment")
    def _compute_display_name(self):
        for record in self:
            provider_label = dict(self.PROVIDERS).get(record.provider, record.provider)
            env_label = dict(self.ENVIRONMENTS).get(record.environment, record.environment)
            record.display_name = f"{provider_label} - {env_label}"

    display_name = fields.Char(
        compute="_compute_display_name",
        store=False,
        help="Nombre para mostrar con proveedor y entorno",
    )

    _sql_constraints = [
        (
            "surpay_provider_config_uniq",
            "unique(provider, environment)",
            "Solo se permite una configuracion por combinacion proveedor/entorno.",
        ),
    ]

    @staticmethod
    def _get_encryption_key():
        """Get or create encryption key from Odoo config."""
        try:
            from odoo.tools import config
            key_str = config.get("surpay_encryption_key")
            if not key_str:
                # Generate new key if not configured
                key = Fernet.generate_key()
                key_str = key.decode("utf-8")
                _logger.warning(
                    "No surpay_encryption_key configured. Generated new key (save to .odoorc): %s",
                    key_str,
                )
            return key_str.encode("utf-8") if isinstance(key_str, str) else key_str
        except Exception as e:
            _logger.error("Failed to get encryption key: %s", e)
            # Fallback: use a default key (not secure for production!)
            return b"NOT_SET_CONFIGURE_SURPAY_ENCRYPTION_KEY_IN_ODOO_CONF_!"

    @staticmethod
    def _encrypt_field(value):
        """Encrypt a field value."""
        if not value:
            return value
        try:
            key = SurpayProviderConfig._get_encryption_key()
            cipher = Fernet(key)
            encrypted = cipher.encrypt(value.encode("utf-8"))
            return base64.b64encode(encrypted).decode("utf-8")
        except Exception as e:
            _logger.error("Encryption failed: %s", e)
            return value

    @staticmethod
    def _decrypt_field(value):
        """Decrypt a field value."""
        if not value:
            return value
        try:
            key = SurpayProviderConfig._get_encryption_key()
            cipher = Fernet(key)
            encrypted = base64.b64decode(value.encode("utf-8"))
            decrypted = cipher.decrypt(encrypted)
            return decrypted.decode("utf-8")
        except Exception as e:
            _logger.error("Decryption failed: %s", e)
            # Return None so callers can fallback gracefully instead of using garbage ciphertext.
            return None

    def get_credentials(self):
        """Get decrypted credentials for this provider config."""
        return {
            "provider": self.provider,
            "environment": self.environment,
            "api_key": self._decrypt_field(self.api_key),
            "customer_uuid": self._decrypt_field(self.customer_uuid),
            "pos_id": self._decrypt_field(self.pos_id),
            "webhook_secret": self._decrypt_field(self.webhook_secret),
            "base_url": self._decrypt_field(self.base_url),
            "webhook_url": self.webhook_url,
        }

    def _validate_credentials(self):
        """Validate that required credentials are present for the provider."""
        if self.provider == "depay":
            if not self.api_key:
                raise ValidationError(_("La API Key de Depay es obligatoria"))
            if not self.customer_uuid:
                raise ValidationError(_("El Customer UUID de Depay es obligatorio"))
            if not self.pos_id:
                raise ValidationError(_("El POS ID de Depay es obligatorio"))
        elif self.provider == "klap":
            if not self.api_key:
                raise ValidationError(_("La API Key de Klap es obligatoria"))

    @api.model_create_multi
    def create(self, vals_list):
        """Override create to validate and encrypt credentials."""
        for vals in vals_list:
            if "api_key" in vals and vals["api_key"]:
                vals["api_key"] = self._encrypt_field(vals["api_key"])
            if "customer_uuid" in vals and vals["customer_uuid"]:
                vals["customer_uuid"] = self._encrypt_field(vals["customer_uuid"])
            if "pos_id" in vals and vals["pos_id"]:
                vals["pos_id"] = self._encrypt_field(vals["pos_id"])
            if "webhook_secret" in vals and vals["webhook_secret"]:
                vals["webhook_secret"] = self._encrypt_field(vals["webhook_secret"])
            if "base_url" in vals and vals["base_url"]:
                vals["base_url"] = self._encrypt_field(vals["base_url"])

        records = super().create(vals_list)
        records._validate_credentials()
        return records

    def write(self, vals):
        """Override write to encrypt credentials on update."""
        if "api_key" in vals and vals["api_key"]:
            vals["api_key"] = self._encrypt_field(vals["api_key"])
        if "customer_uuid" in vals and vals["customer_uuid"]:
            vals["customer_uuid"] = self._encrypt_field(vals["customer_uuid"])
        if "pos_id" in vals and vals["pos_id"]:
            vals["pos_id"] = self._encrypt_field(vals["pos_id"])
        if "webhook_secret" in vals and vals["webhook_secret"]:
            vals["webhook_secret"] = self._encrypt_field(vals["webhook_secret"])
        if "base_url" in vals and vals["base_url"]:
            vals["base_url"] = self._encrypt_field(vals["base_url"])

        result = super().write(vals)
        self._validate_credentials()
        return result

    def action_test_connection(self):
        """Test connection to provider with actual API call."""
        self.ensure_one()
        
        _logger.info(
            "Testing connection for provider=%s, env=%s, id=%s",
            self.provider,
            self.environment,
            self.id,
        )
        
        try:
            # Validate required credentials are present
            self._validate_credentials()
            creds = self.get_credentials()
            
            if self.provider == "depay":
                self._test_depay_connection(creds)
            elif self.provider == "klap":
                self._test_klap_connection(creds)
            else:
                raise UserError(_("La prueba de conexion no esta implementada para %s") % self.provider)
            
            # Update last test date only if successful
            self.last_test_date = fields.Datetime.now()
            _logger.info("Connection test successful for %s", self.display_name)
            
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": _("Conexion exitosa"),
                    "message": _("El proveedor %s (%s) responde correctamente")
                    % (self.provider.upper(), self.environment),
                    "type": "success",
                    "sticky": False,
                },
            }
        except ValidationError as ve:
            _logger.warning("Validation failed for %s: %s", self.display_name, str(ve))
            raise
        except requests.exceptions.Timeout:
            error_msg = _("Tiempo de espera agotado al conectar con API de %s (>20s)") % self.provider
            _logger.exception(error_msg)
            raise UserError(error_msg)
        except requests.exceptions.ConnectionError as e:
            error_msg = _("No se pudo conectar con API de %s: %s") % (self.provider, str(e))
            _logger.exception(error_msg)
            raise UserError(error_msg)
        except Exception as e:
            error_msg = _("La prueba de conexion fallo: %s") % str(e)
            _logger.exception(error_msg)
            raise UserError(error_msg)

    def _test_depay_connection(self, creds):
        """Test actual connection to Depay API with provided credentials."""
        import requests
        
        api_key = (creds.get("api_key") or "").strip()
        customer_uuid = (creds.get("customer_uuid") or "").strip()
        if not api_key:
            raise ValidationError(_("La API key de Depay es obligatoria"))
        
        base_url = (creds.get("base_url") or "https://stage.api.payments.depay.us").rstrip("/")
        timeout = 20
        
        # Build URL based on environment
        if self.environment == "production":
            auth_url = f"{base_url.replace('stage', 'api')}/auth/token"
        else:
            auth_url = f"{base_url}/auth/token"
        
        _logger.info("Testing Depay connection to: %s", auth_url)
        
        headers = {"x-api-key": api_key}
        # Some Depay tenants require customer UUID as an extra header.
        if customer_uuid:
            headers["x-customer-uuid"] = customer_uuid
        
        try:
            response = requests.get(auth_url, headers=headers, timeout=timeout)
            
            _logger.info("Depay API response status: %s", response.status_code)
            _logger.info("Depay API response body: %s", response.text[:500])
            
            # Status 401 = invalid credentials, 200 = success, 400+ = various errors
            if response.status_code == 401:
                raise UserError(_("API key invalida: 401 Unauthorized"))
            elif response.status_code >= 500:
                raise UserError(_("Error de servidor de API Depay: %s") % response.status_code)
            elif response.status_code >= 400:
                try:
                    error_data = response.json()
                    error_msg = error_data.get("message", f"HTTP {response.status_code}")
                except Exception:
                    error_msg = response.text
                raise UserError(
                    _("Autenticacion Depay fallida [%s] en %s: %s")
                    % (response.status_code, auth_url, error_msg)
                )
            
            # Success - validate we got a token
            data = response.json()
            token = data.get("accessToken")
            if not token:
                raise UserError(_("La respuesta de API no incluye accessToken"))
            
            _logger.info("Successfully obtained access token (first 20 chars): %s...", token[:20])
            
        except requests.exceptions.RequestException as e:
            raise UserError(_("La solicitud HTTP fallo: %s") % str(e))

    def _test_klap_connection(self, creds):
        """Test actual connection to Klap API with provided credentials."""
        import requests
        
        api_key = (creds.get("api_key") or "").strip()
        if not api_key:
            raise ValidationError(_("La API key de Klap es obligatoria"))
        
        base_url = (creds.get("base_url") or "https://api.klap.com").rstrip("/")
        timeout = 20
        
        _logger.info("Testing Klap connection to: %s", base_url)
        
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        
        try:
            # Try to get account info as a simple test
            response = requests.get(f"{base_url}/v1/account", headers=headers, timeout=timeout)
            
            _logger.info("Klap API response status: %s", response.status_code)
            _logger.info("Klap API response body: %s", response.text[:500])
            
            if response.status_code == 401:
                raise UserError(_("API key invalida: 401 Unauthorized"))
            elif response.status_code >= 500:
                raise UserError(_("Error de servidor de API Klap: %s") % response.status_code)
            elif response.status_code >= 400:
                try:
                    error_data = response.json()
                    error_msg = error_data.get("message", f"HTTP {response.status_code}")
                except:
                    error_msg = response.text
                raise UserError(_("Error de API Klap: %s") % error_msg)
            
            _logger.info("Successfully connected to Klap API")
            
        except requests.exceptions.RequestException as e:
            raise UserError(_("La solicitud HTTP fallo: %s") % str(e))
