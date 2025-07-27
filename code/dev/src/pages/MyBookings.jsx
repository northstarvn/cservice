import React, { useState, useEffect } from 'react';
import { useAuth } from '../context/AuthContext';
import { useApp } from '../context/AppContext';
import LoadingSpinner from '../components/LoadingSpinner';

// Booking status constants
export const BOOKING_STATUSES = {
  CONFIRMED: 'confirmed',
  PENDING: 'pending',
  COMPLETED: 'completed',
  CANCELLED: 'cancelled',
  RESCHEDULED: 'rescheduled'
};

const MyBookings = () => {
  const { user } = useAuth();
  const { showNotification } = useApp();
  const [bookings, setBookings] = useState([]);
  const [loading, setLoading] = useState(true);
  const [filter, setFilter] = useState('all');
  const [searchTerm, setSearchTerm] = useState('');
  const [sortBy, setSortBy] = useState('newest');

  useEffect(() => {
    loadBookings();
  }, [user]);

  const loadBookings = () => {
    setLoading(true);
    try {
      // Load bookings from localStorage (replace with API call)
      const savedBookings = JSON.parse(localStorage.getItem('userBookings') || '[]');
      const userBookings = savedBookings.filter(booking => booking.user_id === user?.id);
      setBookings(userBookings);
    } catch (error) {
      console.error('Error loading bookings:', error);
      showNotification('Error loading bookings', 'error');
    } finally {
      setLoading(false);
    }
  };

  const canCancelOrReschedule = (booking) => {
    const bookingDateTime = new Date(`${booking.date}T${booking.time}`);
    const now = new Date();
    const twoHoursFromNow = new Date(now.getTime() + 2 * 60 * 60 * 1000);
    return bookingDateTime > twoHoursFromNow && booking.status === BOOKING_STATUSES.CONFIRMED;
  };

  const handleCancel = async (bookingId) => {
    if (!window.confirm('Are you sure you want to cancel this booking?')) return;
    
    try {
      const updatedBookings = bookings.map(booking =>
        booking.booking_id === bookingId
          ? { ...booking, status: BOOKING_STATUSES.CANCELLED }
          : booking
      );
      setBookings(updatedBookings);
      
      // Update localStorage
      const allBookings = JSON.parse(localStorage.getItem('userBookings') || '[]');
      const updated = allBookings.map(booking =>
        booking.booking_id === bookingId
          ? { ...booking, status: BOOKING_STATUSES.CANCELLED }
          : booking
      );
      localStorage.setItem('userBookings', JSON.stringify(updated));
      
      showNotification('Booking cancelled successfully', 'success');
    } catch (error) {
      showNotification('Error cancelling booking', 'error');
    }
  };

  const generateCalendarFile = (booking) => {
    const startDate = new Date(`${booking.date}T${booking.time}`);
    const endDate = new Date(startDate.getTime() + 60 * 60 * 1000); // 1 hour duration
    
    const formatDate = (date) => {
      return date.toISOString().replace(/[-:]/g, '').split('.')[0] + 'Z';
    };

    const icsContent = `BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Your Company//Booking System//EN
BEGIN:VEVENT
UID:${booking.booking_id}@yourcompany.com
DTSTAMP:${formatDate(new Date())}
DTSTART:${formatDate(startDate)}
DTEND:${formatDate(endDate)}
SUMMARY:${booking.service_type} Appointment
DESCRIPTION:Booking ID: ${booking.booking_id}\\nService: ${booking.service_type}\\nNotes: ${booking.notes || 'None'}
END:VEVENT
END:VCALENDAR`;

    const blob = new Blob([icsContent], { type: 'text/calendar' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `booking-${booking.booking_id}.ics`;
    link.click();
    URL.revokeObjectURL(url);
  };

  const filteredBookings = bookings
    .filter(booking => {
      if (filter === 'all') return true;
      if (filter === 'upcoming') return booking.status === BOOKING_STATUSES.CONFIRMED;
      if (filter === 'completed') return booking.status === BOOKING_STATUSES.COMPLETED;
      if (filter === 'cancelled') return booking.status === BOOKING_STATUSES.CANCELLED;
      return true;
    })
    .filter(booking =>
      booking.service_type.toLowerCase().includes(searchTerm.toLowerCase()) ||
      booking.booking_id.toLowerCase().includes(searchTerm.toLowerCase())
    )
    .sort((a, b) => {
      if (sortBy === 'newest') {
        return new Date(b.created_at) - new Date(a.created_at);
      } else if (sortBy === 'oldest') {
        return new Date(a.created_at) - new Date(b.created_at);
      } else if (sortBy === 'date') {
        return new Date(`${a.date}T${a.time}`) - new Date(`${b.date}T${b.time}`);
      }
      return 0;
    });

  const getStatusBadge = (status) => {
    const statusClasses = {
      [BOOKING_STATUSES.CONFIRMED]: 'bg-green-100 text-green-800',
      [BOOKING_STATUSES.PENDING]: 'bg-yellow-100 text-yellow-800',
      [BOOKING_STATUSES.COMPLETED]: 'bg-blue-100 text-blue-800',
      [BOOKING_STATUSES.CANCELLED]: 'bg-red-100 text-red-800',
      [BOOKING_STATUSES.RESCHEDULED]: 'bg-purple-100 text-purple-800'
    };

    return (
      <span className={`px-2 py-1 rounded-full text-xs font-medium ${statusClasses[status]}`}>
        {status.charAt(0).toUpperCase() + status.slice(1)}
      </span>
    );
  };

  if (loading) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <LoadingSpinner size="large" />
      </div>
    );
  }

  return (
    <div className="container mx-auto px-4 py-8 max-w-6xl">
      <div className="mb-8">
        <h1 className="text-3xl font-bold text-gray-900 dark:text-white mb-2">My Bookings</h1>
        <p className="text-gray-600 dark:text-gray-400">Manage your appointments and booking history</p>
      </div>

      {/* Filters and Search */}
      <div className="bg-white dark:bg-gray-800 rounded-lg shadow-sm p-6 mb-6">
        <div className="flex flex-col md:flex-row gap-4">
          <div className="flex-1">
            <input
              type="text"
              placeholder="Search by service type or booking ID..."
              value={searchTerm}
              onChange={(e) => setSearchTerm(e.target.value)}
              className="w-full px-4 py-2 border border-gray-300 dark:border-gray-600 rounded-lg focus:ring-2 focus:ring-blue-500 dark:bg-gray-700 dark:text-white"
            />
          </div>
          <div className="flex gap-4">
            <select
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
              className="px-4 py-2 border border-gray-300 dark:border-gray-600 rounded-lg focus:ring-2 focus:ring-blue-500 dark:bg-gray-700 dark:text-white"
            >
              <option value="all">All Bookings</option>
              <option value="upcoming">Upcoming</option>
              <option value="completed">Completed</option>
              <option value="cancelled">Cancelled</option>
            </select>
            <select
              value={sortBy}
              onChange={(e) => setSortBy(e.target.value)}
              className="px-4 py-2 border border-gray-300 dark:border-gray-600 rounded-lg focus:ring-2 focus:ring-blue-500 dark:bg-gray-700 dark:text-white"
            >
              <option value="newest">Newest First</option>
              <option value="oldest">Oldest First</option>
              <option value="date">By Date</option>
            </select>
          </div>
        </div>
      </div>

      {/* Bookings List */}
      {filteredBookings.length === 0 ? (
        <div className="bg-white dark:bg-gray-800 rounded-lg shadow-sm p-12 text-center">
          <div className="mx-auto w-16 h-16 bg-gray-100 dark:bg-gray-700 rounded-full flex items-center justify-center mb-4">
            <svg className="w-8 h-8 text-gray-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 7V3m8 4V3m-9 8h10M5 21h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z" />
            </svg>
          </div>
          <h3 className="text-lg font-medium text-gray-900 dark:text-white mb-2">No bookings found</h3>
          <p className="text-gray-600 dark:text-gray-400 mb-6">You haven't made any bookings yet or no bookings match your search.</p>
          <button
            onClick={() => window.location.href = '/booking'}
            className="bg-blue-600 text-white px-6 py-2 rounded-lg font-medium hover:bg-blue-700 transition-colors"
          >
            Make a Booking
          </button>
        </div>
      ) : (
        <div className="grid gap-6">
          {filteredBookings.map((booking) => (
            <div key={booking.booking_id} className="bg-white dark:bg-gray-800 rounded-lg shadow-sm border border-gray-200 dark:border-gray-700 p-6">
              <div className="flex flex-col md:flex-row md:items-center justify-between mb-4">
                <div className="flex items-center gap-3 mb-2 md:mb-0">
                  <h3 className="text-lg font-semibold text-gray-900 dark:text-white">
                    {booking.service_type}
                  </h3>
                  {getStatusBadge(booking.status)}
                </div>
                <div className="text-sm text-gray-600 dark:text-gray-400">
                  ID: {booking.booking_id}
                </div>
              </div>

              <div className="grid md:grid-cols-2 gap-4 mb-4">
                <div>
                  <div className="text-sm text-gray-600 dark:text-gray-400">Date & Time</div>
                  <div className="font-medium text-gray-900 dark:text-white">
                    {new Date(booking.date).toLocaleDateString('en-US', {
                      weekday: 'long',
                      year: 'numeric',
                      month: 'long',
                      day: 'numeric'
                    })} at {new Date(`2000-01-01T${booking.time}`).toLocaleTimeString('en-US', {
                      hour: '2-digit',
                      minute: '2-digit',
                      hour12: true
                    })}
                  </div>
                </div>
                <div>
                  <div className="text-sm text-gray-600 dark:text-gray-400">Created</div>
                  <div className="font-medium text-gray-900 dark:text-white">
                    {new Date(booking.created_at).toLocaleDateString()}
                  </div>
                </div>
              </div>

              {booking.notes && (
                <div className="mb-4">
                  <div className="text-sm text-gray-600 dark:text-gray-400">Notes</div>
                  <div className="text-gray-900 dark:text-white">{booking.notes}</div>
                </div>
              )}

              <div className="flex flex-wrap gap-2">
                <button
                  onClick={() => generateCalendarFile(booking)}
                  className="px-4 py-2 text-sm border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-300 rounded-lg hover:bg-gray-50 dark:hover:bg-gray-700 transition-colors"
                >
                  Add to Calendar
                </button>
                {canCancelOrReschedule(booking) && (
                  <button
                    onClick={() => handleCancel(booking.booking_id)}
                    className="px-4 py-2 text-sm border border-red-300 text-red-700 rounded-lg hover:bg-red-50 transition-colors"
                  >
                    Cancel
                  </button>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
};

export default MyBookings;
