# Integracion API Externa Multi-proveedor (Sin Legacy)

Este documento describe el contrato vigente para la API externa de pagos en Surpay.

## Resumen

- `provider` es obligatorio en cada `POST /api/v1/payments/intents`.
- No existe fallback implicito a `depay`.
- La configuracion efectiva se resuelve por esta regla:
  1. Override del cliente para ese proveedor.
  2. Configuracion activa global para ese proveedor.
  3. Error `provider_not_configured` si no existe ninguna.

## Modelo de configuracion por cliente

En `surpay.api.client` ya no se usa una configuracion unica de proveedor.
Ahora se administra una lista de overrides por proveedor:

- Modelo: `surpay.api.client.provider.override`
- Claves: `client_id`, `provider`, `provider_config_id`
- Restriccion: unico por `client_id + provider`

El override puede apuntar a una configuracion inactiva cuando se requiere pruebas controladas por cliente.

## Endpoints relevantes

- Crear intent: `POST /api/v1/payments/intents`
- Consultar intent: `GET /api/v1/payments/intents/{order_id}`
- Webhook proveedor: `POST /api/v1/webhooks/providers/{provider}`
- Extra data: `POST /api/v1/payments/extra-data`

## Errores de contrato

- `missing_provider`: falta `provider` en el payload.
- `unsupported_provider`: proveedor no soportado por catalogo.
- `provider_service_not_available`: proveedor soportado pero sin servicio implementado.
- `provider_not_configured`: no hay override ni configuracion global activa.
- `provider_mismatch_webhook`: provider de la ruta no coincide con el intent.

## Ejemplo minimo create intent

```json
{
  "provider": "depay",
  "amount": 12000,
  "currency": "CLP",
  "external_order_id": "txn_1234567890",
  "qr_from": "AR"
}
```

## Integrador de referencia (Unimarc)

Unimarc debe mapear el selector frontend `payment_method` al campo `provider` del request al gateway.
La seleccion es por transaccion, no por cliente fijo.
