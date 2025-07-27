import React from 'react';
import { useNavigate } from 'react-router-dom';

const BookingConfirmationPopup = ({ isOpen, onClose, bookingDetails }) => {
  const navigate = useNavigate();
  
  if (!isOpen) return null;

  const handleViewBookings = () => {
    onClose();
    navigate('/bookings');
  };

  const formatDate = (dateString) => {
    const date = new Date(dateString);
    return date.toLocaleDateString('en-US', {
      weekday: 'long',
      year: 'numeric',
      month: 'long',
      day: 'numeric'
    });
  };

  const formatTime = (timeString) => {
    const time = new Date(`2000-01-01T${timeString}`);
    return time.toLocaleTimeString('en-US', {
      hour: '2-digit',
      minute: '2-digit',
      hour12: true
    });
  };

  return (
    <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
      <div className="bg-white rounded-2xl shadow-2xl p-8 w-full max-w-md mx-4 transform transition-all duration-300 scale-100">
        <div className="text-center mb-6">
          <div className="mx-auto w-16 h-16 bg-green-100 rounded-full flex items-center justify-center mb-4">
            <svg className="w-8 h-8 text-green-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
            </svg>
          </div>
          <h2 className="text-2xl font-bold text-gray-900 mb-2">Booking Confirmed!</h2>
          <p className="text-gray-600">Your booking has been successfully confirmed. Details have been sent to your email.</p>
        </div>

        {bookingDetails && (
          <div className="bg-gray-50 rounded-lg p-4 mb-6">
            <h3 className="font-semibold text-gray-900 mb-3">Booking Details</h3>
            <div className="space-y-2">
              <div className="flex justify-between">
                <span className="text-gray-600">Booking ID:</span>
                <span className="font-medium text-gray-900">{bookingDetails.bookingId}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-gray-600">Service:</span>
                <span className="font-medium text-gray-900">{bookingDetails.serviceType}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-gray-600">Date:</span>
                <span className="font-medium text-gray-900">{formatDate(bookingDetails.date)}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-gray-600">Time:</span>
                <span className="font-medium text-gray-900">{formatTime(bookingDetails.time)}</span>
              </div>
              {bookingDetails.notes && (
                <div className="flex justify-between">
                  <span className="text-gray-600">Notes:</span>
                  <span className="font-medium text-gray-900">{bookingDetails.notes}</span>
                </div>
              )}
            </div>
          </div>
        )}

        <div className="space-y-3">
          <button
            onClick={handleViewBookings}
            className="w-full bg-blue-600 text-white py-3 px-4 rounded-lg font-medium hover:bg-blue-700 transition-colors"
          >
            View My Bookings
          </button>
          <button
            onClick={onClose}
            className="w-full border border-gray-300 text-gray-700 py-3 px-4 rounded-lg font-medium hover:bg-gray-50 transition-colors"
          >
            OK
          </button>
        </div>

        <div className="mt-4 flex items-center text-sm text-gray-600">
          <svg className="w-4 h-4 mr-2" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
          </svg>
          <span>You can manage your booking anytime in "My Bookings"</span>
        </div>
      </div>
    </div>
  );
};

export default BookingConfirmationPopup;