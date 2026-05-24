import frappe
from cowberry.utils.response import err


class CowberryError(Exception):
    code = "COWBERRY_ERROR"
    message = "An unexpected error occurred."
    http_status = 400

    def __init__(self, message=None, **kwargs):
        self.message = message or self.__class__.message
        self.kwargs = kwargs
        super().__init__(self.message)

    def to_response(self):
        return err(self.code, self.message, self.http_status, **self.kwargs)


class NotDriverError(CowberryError):
    code = "NOT_DRIVER"
    message = "Access restricted to drivers."
    http_status = 403


class OTPExpiredError(CowberryError):
    code = "OTP_EXPIRED"
    message = "OTP has expired."
    http_status = 400


class OTPInvalidError(CowberryError):
    code = "OTP_INVALID"
    message = "OTP is invalid."
    http_status = 400


class OTPMaxAttemptsError(CowberryError):
    code = "OTP_MAX_ATTEMPTS"
    message = "Maximum OTP attempts exceeded."
    http_status = 429


class OrderNotFoundError(CowberryError):
    code = "ORDER_NOT_FOUND"
    message = "Order not found."
    http_status = 404


class DeliveryNoteNotFoundError(CowberryError):
    code = "DELIVERY_NOTE_NOT_FOUND"
    message = "Delivery note not found."
    http_status = 404


class PaymentNotConfirmedError(CowberryError):
    code = "PAYMENT_NOT_CONFIRMED"
    message = "Payment has not been confirmed."
    http_status = 402


class InvalidStatusTransitionError(CowberryError):
    code = "INVALID_STATUS_TRANSITION"
    message = "Invalid status transition."
    http_status = 400


class CollectionAlreadySubmittedError(CowberryError):
    code = "COLLECTION_ALREADY_SUBMITTED"
    message = "Collection has already been submitted."
    http_status = 400


class WalletInsufficientError(CowberryError):
    code = "WALLET_INSUFFICIENT"
    message = "Insufficient wallet balance."
    http_status = 400


class InvalidCoordinatesError(CowberryError):
    code = "INVALID_COORDINATES"
    message = "Invalid coordinates provided."
    http_status = 400


class RazorpayWebhookError(CowberryError):
    code = "RAZORPAY_WEBHOOK_ERROR"
    message = "Razorpay webhook verification failed."
    http_status = 400
