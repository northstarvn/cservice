import React, { useState, useEffect, useMemo, useCallback } from 'react';
import { useAuth } from '../context/AuthContext';
import { useApp } from '../context/AppContext';
import LoadingSpinner from '../components/LoadingSpinner';
import { BookingListSkeleton } from '../components/LoadingSkeletons';
import { Calendar, Clock, User, Package, AlertCircle, CheckCircle, XCircle, RotateCcw } from 'lucide-react';

// API service for backend integration
const API_BASE_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000';

class BookingApiService {
  constructor() {
    this.baseURL = API_BASE_URL;
  }

  async getAuthHeaders() {
    const token = localStorage.getItem('authToken');
    return {
      'Content-Type': 'application/json',
      ...(token && { Authorization: `Bearer ${token}` }),
    };
  }

  async request(endpoint, options = {}) {
    const headers = await this.getAuthHeaders();
    const config = {
      headers: { ...headers, ...options.headers },
      ...options,
    };

    if (config.body && typeof config.body === 'object') {
      config.body = JSON.stringify(config.body);
    }

    try {
      const response = await fetch(`${this.baseURL}${endpoint}`, config);
      
      if (!response.ok) {
        let errorMessage = `HTTP error! status: ${response.status}`;
        try {
          const errorData = await response.json();
          errorMessage = errorData.detail || errorMessage;
        } catch (e) {
          // If we can't parse the error response, use the status message
        }
        throw new Error(errorMessage);
      }
      
      // Handle 204 No Content responses
      if (response.status === 204) {
        return null;
      }
      
      return await response.json();
    } catch (error) {
      console.error('API request failed:', error);
      throw error;
    }
  }

  async getBookings(params = {}) {
    const queryString = new URLSearchParams(params).toString();
    return this.request(`/bookings?${queryString}`);
  }

  async deleteBooking(bookingId) {
    return this.request(`/bookings/${bookingId}`, {
      method: 'DELETE',
    });
  }

  async updateBooking(bookingId, updateData) {
    return this.request(`/bookings/${bookingId}`, {
      method: 'PATCH',
      body: updateData,
    });
  }
}

const bookingApi = new BookingApiService();

// Constants
const BOOKING_STATUSES = {
  PENDING: 'pending',
  CONFIRMED: 'confirmed', 
  COMPLETED: 'completed',
  CANCELLED: 'cancelled',
  RESCHEDULED: 'rescheduled'
};

const STATUS_COLORS = {
  [BOOKING_STATUSES.PENDING]: 'bg-yellow-100 text-yellow-800',
  [BOOKING_STATUSES.CONFIRMED]: 'bg-green-100 text-green-800',
  [BOOKING_STATUSES.COMPLETED]: 'bg-blue-100 text-blue-800',
  [BOOKING_STATUSES.CANCELLED]: 'bg-red-100 text-red-800',
  [BOOKING_STATUSES.RESCHEDULED]: 'bg-purple-100 text-purple-800'
};

const STATUS_ICONS = {
  [BOOKING_STATUSES.PENDING]: AlertCircle,
  [BOOKING_STATUSES.CONFIRMED]: CheckCircle,
  [BOOKING_STATUSES.COMPLETED]: CheckCircle,
  [BOOKING_STATUSES.CANCELLED]: XCircle,
  [BOOKING_STATUSES.RESCHEDULED]: RotateCcw
};

const MyBookings = () => {
  const { user } = useAuth();
  const { showNotification } = useApp();
  const [bookings, setBookings] = useState([]);
  const [loading, setLoading] = useState(true);
  const [filter, setFilter] = useState('all');
  const [searchTerm, setSearchTerm] = useState('');
  const [sortBy, setSortBy] = useState('newest');
  const [optimisticUpdates, setOptimisticUpdates] = useState(new Set());
  const [error, setError] = useState(null);

  // Memoized filtered bookings for performance
  const filteredBookings = useMemo(() => {
    return bookings
      .filter(booking => {
        if (filter === 'all') return true;
        if (filter === 'upcoming') return ['confirmed', 'pending'].includes(booking.status);
        if (filter === 'completed') return booking.status === 'completed';
        if (filter === 'cancelled') return booking.status === 'cancelled';
        return true;
      })
      .filter(booking =>
        booking.service_type?.toLowerCase().includes(searchTerm.toLowerCase()) ||
        booking.id?.toString().includes(searchTerm.toLowerCase()) ||
        booking.notes?.toLowerCase().includes(searchTerm.toLowerCase())
      )
      .sort((a, b) => {
        if (sortBy === 'newest') {
          return new Date(b.created_at) - new Date(a.created_at);
        } else if (sortBy === 'oldest') {
          return new Date(a.created_at) - new Date(b.created_at);
        } else if (sortBy === 'date') {
          return new Date(a.scheduled_for) - new Date(b.scheduled_for);
        }
        return 0;
      });
  }, [bookings, filter, searchTerm, sortBy]);

  // Load bookings from backend
  const loadBookings = useCallback(async () => {
    if (!user?.id) return;
    
    setLoading(true);
    setError(null);
    
    try {
      const data = await bookingApi.getBookings({
        limit: 100, // Adjust as needed
        skip: 0
      });
      
      // Handle both array response and paginated response
      const bookingsList = Array.isArray(data) ? data : data.items || [];
      setBookings(bookingsList);
    } catch (error) {
      console.error('Error loading bookings:', error);
      setError(error.message);
      
      if (showNotification) {
        showNotification(`Error loading bookings: ${error.message}`, 'error');
      }
      
      // Fallback to localStorage if backend fails
      try {
        const savedBookings = JSON.parse(localStorage.getItem('userBookings') || '[]');
        const userBookings = savedBookings.filter(booking => booking.user_id === user?.id);
        setBookings(userBookings);
        
        if (showNotification) {
          showNotification('Loaded bookings from local storage as fallback', 'warning');
        }
      } catch (fallbackError) {
        console.error('Fallback to localStorage also failed:', fallbackError);
      }
    } finally {
      setLoading(false);
    }
  }, [user?.id, showNotification]);

  // Optimistic update for cancellation with real backend call
  const handleCancelOptimistic = useCallback(async (bookingId) => {
    if (!window.confirm('Are you sure you want to cancel this booking?')) return;
    
    setOptimisticUpdates(prev => new Set(prev).add(bookingId));
    
    // Update UI immediately (optimistic update)
    const originalBooking = bookings.find(b => b.id === bookingId);
    setBookings(prev => prev.map(booking =>
      booking.id === bookingId
        ? { ...booking, status: BOOKING_STATUSES.CANCELLED }
        : booking
    ));
    
    try {
      // Real backend API call
      await bookingApi.deleteBooking(bookingId);
      
      if (showNotification) {
        showNotification('Booking cancelled successfully', 'success');
      }
      
      // Refresh bookings to get latest state from server
      await loadBookings();
    } catch (error) {
      console.error('Error cancelling booking:', error);
      
      // Revert optimistic update on error
      setBookings(prev => prev.map(booking =>
        booking.id === bookingId
          ? originalBooking
          : booking
      ));
      
      if (showNotification) {
        showNotification(`Failed to cancel booking: ${error.message}`, 'error');
      }
    } finally {
      setOptimisticUpdates(prev => {
        const newSet = new Set(prev);
        newSet.delete(bookingId);
        return newSet;
      });
    }
  }, [bookings, showNotification, loadBookings]);

  // Format date and time
  const formatDateTime = (scheduledFor) => {
    try {
      const date = new Date(scheduledFor);
      return {
        date: date.toLocaleDateString('en-US', {
          year: 'numeric',
          month: 'long',
          day: 'numeric'
        }),
        time: date.toLocaleTimeString('en-US', {
          hour: '2-digit',
          minute: '2-digit'
        })
      };
    } catch (error) {
      return { date: 'Invalid date', time: 'Invalid time' };
    }
  };

  // Booking card component
  const BookingCard = ({ booking }) => {
    const StatusIcon = STATUS_ICONS[booking.status] || AlertCircle;
    const { date, time } = formatDateTime(booking.scheduled_for);
    const isProcessing = optimisticUpdates.has(booking.id);
    
    return (
      <div className="bg-white dark:bg-gray-800 rounded-lg shadow-sm border border-gray-200 dark:border-gray-700 p-6 hover:shadow-md transition-shadow">
        <div className="flex justify-between items-start mb-4">
          <div className="flex items-center gap-3">
            <div className="flex items-center gap-2">
              <Package className="h-5 w-5 text-gray-500" />
              <h3 className="font-semibold text-gray-900 dark:text-white capitalize">
                {booking.service_type || 'Unknown Service'}
              </h3>
            </div>
            <span className={`inline-flex items-center gap-1 px-2.5 py-0.5 rounded-full text-xs font-medium ${STATUS_COLORS[booking.status]}`}>
              <StatusIcon className="h-3 w-3" />
              {booking.status}
            </span>
          </div>
          <span className="text-sm text-gray-500">
            ID: {booking.id}
          </span>
        </div>
        
        <div className="grid md:grid-cols-2 gap-4 mb-4">
          <div>
            <div className="flex items-center gap-2 text-sm text-gray-600 dark:text-gray-400 mb-1">
              <Calendar className="h-4 w-4" />
              <span>Date</span>
            </div>
            <p className="font-medium text-gray-900 dark:text-white">{date}</p>
          </div>
          <div>
            <div className="flex items-center gap-2 text-sm text-gray-600 dark:text-gray-400 mb-1">
              <Clock className="h-4 w-4" />
              <span>Time</span>
            </div>
            <p className="font-medium text-gray-900 dark:text-white">{time}</p>
          </div>
        </div>
        
        {booking.notes && (
          <div className="mb-4">
            <p className="text-sm text-gray-600 dark:text-gray-400 mb-1">Notes</p>
            <p className="text-gray-900 dark:text-white">{booking.notes}</p>
          </div>
        )}
        
        <div className="flex gap-2">
          {booking.status === BOOKING_STATUSES.CONFIRMED && (
            <button
              onClick={() => handleCancelOptimistic(booking.id)}
              disabled={isProcessing}
              className="px-4 py-2 text-sm font-medium text-red-600 bg-red-50 hover:bg-red-100 rounded-md transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {isProcessing ? 'Cancelling...' : 'Cancel Booking'}
            </button>
          )}
          <button
            className="px-4 py-2 text-sm font-medium text-blue-600 bg-blue-50 hover:bg-blue-100 rounded-md transition-colors"
            onClick={() => {
              // TODO: Implement reschedule functionality
              showNotification && showNotification('Reschedule feature coming soon!', 'info');
            }}
          >
            Reschedule
          </button>
        </div>
      </div>
    );
  };

  useEffect(() => {
    loadBookings();
  }, [loadBookings]);

  if (loading) {
    return (
      <div className="container mx-auto px-4 py-8 max-w-6xl">
        <div className="mb-8">
          <div className="h-8 bg-gray-200 dark:bg-gray-700 rounded w-48 mb-2 animate-pulse"></div>
          <div className="h-4 bg-gray-200 dark:bg-gray-700 rounded w-64 animate-pulse"></div>
        </div>
        <BookingListSkeleton />
      </div>
    );
  }

  return (
    <div className="container mx-auto px-4 py-8 max-w-6xl">
      <div className="mb-8">
        <h1 className="text-3xl font-bold text-gray-900 dark:text-white mb-2">
          My Bookings
        </h1>
        <p className="text-gray-600 dark:text-gray-400">
          Manage your service bookings and appointments
        </p>
      </div>

      {error && (
        <div className="mb-6 p-4 bg-red-50 border border-red-200 rounded-lg">
          <div className="flex items-center gap-2 text-red-800">
            <AlertCircle className="h-5 w-5" />
            <span className="font-medium">Error loading bookings</span>
          </div>
          <p className="text-red-700 mt-1">{error}</p>
          <button
            onClick={loadBookings}
            className="mt-3 px-4 py-2 bg-red-600 text-white rounded-md hover:bg-red-700 transition-colors"
          >
            Retry
          </button>
        </div>
      )}

      {/* Filters and Search */}
      <div className="mb-6 flex flex-col md:flex-row gap-4">
        <div className="flex-1">
          <input
            type="text"
            placeholder="Search bookings..."
            value={searchTerm}
            onChange={(e) => setSearchTerm(e.target.value)}
            className="w-full px-4 py-2 border border-gray-300 dark:border-gray-600 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-blue-500 dark:bg-gray-700 dark:text-white"
          />
        </div>
        
        <select
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          className="px-4 py-2 border border-gray-300 dark:border-gray-600 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-blue-500 dark:bg-gray-700 dark:text-white"
        >
          <option value="all">All Bookings</option>
          <option value="upcoming">Upcoming</option>
          <option value="completed">Completed</option>
          <option value="cancelled">Cancelled</option>
        </select>
        
        <select
          value={sortBy}
          onChange={(e) => setSortBy(e.target.value)}
          className="px-4 py-2 border border-gray-300 dark:border-gray-600 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-blue-500 dark:bg-gray-700 dark:text-white"
        >
          <option value="newest">Newest First</option>
          <option value="oldest">Oldest First</option>
          <option value="date">By Date</option>
        </select>
      </div>

      {/* Bookings List */}
      {filteredBookings.length === 0 ? (
        <div className="text-center py-12">
          <Package className="h-16 w-16 text-gray-400 mx-auto mb-4" />
          <h3 className="text-lg font-medium text-gray-900 dark:text-white mb-2">
            {searchTerm || filter !== 'all' ? 'No matching bookings found' : 'No bookings yet'}
          </h3>
          <p className="text-gray-600 dark:text-gray-400">
            {searchTerm || filter !== 'all' 
              ? 'Try adjusting your search or filter criteria'
              : 'Book your first service to get started'
            }
          </p>
        </div>
      ) : (
        <div className="grid gap-6">
          {filteredBookings.map((booking) => (
            <BookingCard key={booking.id} booking={booking} />
          ))}
        </div>
      )}
    </div>
  );
};

export default MyBookings;