from odoo import api, models


class SurpayApiRequestError(Exception):
    """Error de validación de request en la API externa, con código HTTP y código de error del contrato."""

    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class SurpayProviderService(models.AbstractModel):
    """Contrato común de los servicios de proveedor (surpay.<provider>.api).

    Cada addon de proveedor hereda de este modelo y sobreescribe lo que necesita.
    surpay_base solo conversa con los proveedores a través de estos métodos.
    """

    _name = "surpay.provider.service"
    _description = "Contrato de servicio de proveedor Surpay"

    PROVIDER_ALIASES = {
        "surpay_fronterizo": "depay",
    }

    @api.model
    def normalize_provider(self, provider):
        provider = (provider or "").strip().lower()
        return self.PROVIDER_ALIASES.get(provider, provider)

    @api.model
    def for_provider(self, provider):
        """Retorna el servicio instalado para el proveedor, o None si no hay addon que lo implemente."""
        provider = self.normalize_provider(provider)
        if not provider:
            return None
        service_name = f"surpay.{provider}.api"
        if service_name not in self.env:
            return None
        return self.env[service_name].sudo()

    # ------------------------------------------------------------------
    # Operaciones con el proveedor
    # ------------------------------------------------------------------
    def create_payment(self, payload, provider_config=None):
        raise NotImplementedError()

    def supports_remote_status(self, provider_config=None):
        return True

    def get_payment_status(self, provider_order_id, provider_config=None):
        raise NotImplementedError()

    def map_status(self, status, message=""):
        raise NotImplementedError()

    def pending_timeout_seconds(self, provider_config=None):
        """Segundos tras el despacho en que un pago sin resultado se da por fallido. 0 = sin timeout."""
        return 0

    def on_pending_timeout(self, intent):
        """Acción en el proveedor al vencer el timeout (p. ej. abortar en el terminal). Best effort."""
        return None

    # ------------------------------------------------------------------
    # Lectura de payloads del proveedor
    # ------------------------------------------------------------------
    @staticmethod
    def extract_status(payload):
        data = payload if isinstance(payload, dict) else {}
        return data.get("order_status") or data.get("status") or ""

    @staticmethod
    def extract_status_message(payload):
        data = payload if isinstance(payload, dict) else {}
        return data.get("message") or data.get("detail") or ""

    @staticmethod
    def extract_event_reference(payload):
        data = payload if isinstance(payload, dict) else {}
        return {
            "provider_order_id": data.get("order_id") or "",
            "client_transaction_id": data.get("client_transaction_id") or "",
        }

    def extract_payment_quote(self, payload, fallback_currency="", fallback_amount=0.0):
        """Valores de cotización a guardar en el intent (p. ej. QR con conversión). Por defecto ninguno."""
        return {}

    @staticmethod
    def should_validate_callback_signature():
        return True

    def validate_callback_signature(self, raw_body, signature_header, provider_config=None):
        return False

    # ------------------------------------------------------------------
    # Hooks de la API externa /api/v1/payments/intents
    # ------------------------------------------------------------------
    def api_default_currency(self, client):
        return client.default_local_currency

    def api_validate_intent_request(self, payload, client, provider_config, currency):
        """Valida campos propios del proveedor. Lanza SurpayApiRequestError si el request no es válido."""
        return None

    def api_prepare_intent_vals(self, payload, client, provider_config):
        """Valores extra para crear el surpay.payment.intent."""
        return {}

    def api_build_provider_payload(self, intent, payload, client, provider_config, provider_payload):
        """Completa el payload que se enviará a create_payment."""
        return provider_payload

    def api_commit_before_provider_call(self):
        """True si el intent debe persistirse antes de llamar al proveedor (webhooks que llegan antes del commit)."""
        return False

    def api_response_extras(self, intent, provider_response):
        """Campos adicionales para la respuesta 201 de create intent."""
        return {}
