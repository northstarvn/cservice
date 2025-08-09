import React, { useState, useEffect } from 'react';
import { useAuth } from '../context/AuthContext';
import { useApp } from '../context/AppContext';
import { bookingApi, apiUtils } from '../utils/api'; // Using your existing API structure
import { 
  Calendar, 
  Clock, 
  Package, 
  MessageCircle, 
  Plus, 
  Edit, 
  Trash2,
  Search,
  Filter
} from 'lucide-react';

const Booking = () => {
  const { user } = useAuth();
  const { translations } = useApp();
  const [bookings, setBookings] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [showCreateForm, setShowCreateForm] = useState(false);
  const [editingBooking, setEditingBooking] = useState(null);
  const [pagination, setPagination] = useState({
    page: 1,
    per_page: 10,
    total: 0,
    pages: 0
  });
  const [filters, setFilters] = useState({
    status: '',
    service_type: ''
  });
  const [formData, setFormData] = useState({
    service_type: 'consultation',
    details: '',
    scheduled_for: ''
  });

  const serviceTypes = [
    { value: 'consultation', label: 'Consultation', icon: MessageCircle },
    { value: 'delivery', label: 'Delivery', icon: Package },
    { value: 'meeting', label: 'Meeting', icon: Calendar },
    { value: 'project', label: 'Project Planning', icon: Clock }
  ];

  const statusOptions = [
    { value: '', label: 'All Status' },
    { value: 'pending', label: 'Pending' },
    { value: 'confirmed', label: 'Confirmed' },
    { value: 'cancelled', label: 'Cancelled' },
    { value: 'completed', label: 'Completed' }
  ];

  const fetchBookings = async (page = 1, newFilters = filters) => {
    if (!user || !apiUtils.isAuthenticated()) return;
    
    setLoading(true);
    setError(null);
    try {
      const params = {
        page,
        per_page: pagination.per_page,
        ...newFilters
      };
      
      // Remove empty filter values
      Object.keys(params).forEach(key => {
        if (params[key] === '') delete params[key];
      });
      
      const data = await bookingApi.getBookings(params);
      
      // Handle both paginated and non-paginated responses
      if (data.items) {
        setBookings(data.items);
        setPagination({
          page: data.page,
          per_page: data.per_page,
          total: data.total,
          pages: data.pages
        });
      } else {
        // Legacy response format
        setBookings(Array.isArray(data) ? data : []);
        setPagination(prev => ({ ...prev, total: Array.isArray(data) ? data.length : 0 }));
      }
    } catch (err) {
      const errorInfo = apiUtils.handleApiError(err);
      setError(errorInfo.message);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchBookings();
  }, [user]);

  const handleSubmit = async (e) => {
    e.preventDefault();
    setLoading(true);
    setError(null);
    
    // Validate booking data using your API utils
    const validation = apiUtils.validateBookingData(formData);
    if (!validation.isValid) {
      setError(validation.errors.join(', '));
      setLoading(false);
      return;
    }
    
    try {
      if (editingBooking) {
        await bookingApi.updateBooking(editingBooking.id, formData);
        setEditingBooking(null);
      } else {
        await bookingApi.createBooking(formData);
        setShowCreateForm(false);
      }
      setFormData({
        service_type: 'consultation',
        details: '',
        scheduled_for: ''
      });
      fetchBookings(pagination.page);
    } catch (err) {
      const errorInfo = apiUtils.handleApiError(err);
      setError(errorInfo.message);
    } finally {
      setLoading(false);
    }
  };

  const handleEdit = (booking) => {
    setEditingBooking(booking);
    setFormData({
      service_type: booking.service_type,
      details: booking.details || '',
      scheduled_for: apiUtils.parseBookingDate(booking.scheduled_for).toISOString().slice(0, 16)
    });
    setShowCreateForm(true);
  };

  const handleDelete = async (bookingId) => {
    if (window.confirm('Are you sure you want to delete this booking?')) {
      try {
        await bookingApi.deleteBooking(bookingId);
        fetchBookings(pagination.page);
      } catch (err) {
        const errorInfo = apiUtils.handleApiError(err);
        setError(errorInfo.message);
      }
    }
  };

  const handleFilterChange = (newFilters) => {
    setFilters(newFilters);
    fetchBookings(1, newFilters);
  };

  const handlePageChange = (newPage) => {
    fetchBookings(newPage);
  };

  if (!user || !apiUtils.isAuthenticated()) {
    return (
      <div className="min-h-screen bg-gray-50 flex items-center justify-center">
        <div className="text-center">
          <h2 className="text-2xl font-bold text-gray-900 mb-4">Please Login</h2>
          <p className="text-gray-600">You need to be logged in to view your bookings.</p>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-gray-50 py-8">
      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
        {/* Header */}
        <div className="flex justify-between items-center mb-8">
          <h1 className="text-3xl font-bold text-gray-900">My Bookings</h1>
          <button
            onClick={() => setShowCreateForm(true)}
            className="bg-blue-600 text-white px-4 py-2 rounded-lg hover:bg-blue-700 flex items-center space-x-2"
          >
            <Plus size={20} />
            <span>New Booking</span>
          </button>
        </div>

        {/* Filters */}
        <div className="bg-white rounded-lg shadow p-6 mb-8">
          <div className="flex items-center space-x-4">
            <Filter size={20} className="text-gray-600" />
            <select
              value={filters.status}
              onChange={(e) => handleFilterChange({...filters, status: e.target.value})}
              className="border border-gray-300 rounded-lg px-3 py-2"
            >
              {statusOptions.map(option => (
                <option key={option.value} value={option.value}>{option.label}</option>
              ))}
            </select>
            <select
              value={filters.service_type}
              onChange={(e) => handleFilterChange({...filters, service_type: e.target.value})}
              className="border border-gray-300 rounded-lg px-3 py-2"
            >
              <option value="">All Services</option>
              {serviceTypes.map(type => (
                <option key={type.value} value={type.value}>{type.label}</option>
              ))}
            </select>
          </div>
        </div>

        {/* Create/Edit Form */}
        {showCreateForm && (
          <div className="bg-white rounded-lg shadow p-6 mb-8">
            <h3 className="text-lg font-semibold mb-4">
              {editingBooking ? 'Edit Booking' : 'Create New Booking'}
            </h3>
            <form onSubmit={handleSubmit} className="grid grid-cols-1 md:grid-cols-2 gap-4">
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-2">
                  Service Type
                </label>
                <select
                  value={formData.service_type}
                  onChange={(e) => setFormData({...formData, service_type: e.target.value})}
                  className="w-full p-3 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500"
                  required
                >
                  {serviceTypes.map(type => (
                    <option key={type.value} value={type.value}>{type.label}</option>
                  ))}
                </select>
              </div>

              <div>
                <label className="block text-sm font-medium text-gray-700 mb-2">
                  Scheduled Date & Time
                </label>
                <input
                  type="datetime-local"
                  value={formData.scheduled_for}
                  onChange={(e) => setFormData({...formData, scheduled_for: e.target.value})}
                  className="w-full p-3 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500"
                  required
                />
              </div>

              <div className="md:col-span-2">
                <label className="block text-sm font-medium text-gray-700 mb-2">
                  Details (Optional)
                </label>
                <textarea
                  value={formData.details}
                  onChange={(e) => setFormData({...formData, details: e.target.value})}
                  className="w-full p-3 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500"
                  rows="3"
                  placeholder="Additional details about your booking"
                />
              </div>

              <div className="md:col-span-2 flex space-x-4">
                <button
                  type="submit"
                  disabled={loading}
                  className="bg-blue-600 text-white px-6 py-2 rounded-lg hover:bg-blue-700 disabled:opacity-50"
                >
                  {loading ? 'Saving...' : (editingBooking ? 'Update' : 'Create')} Booking
                </button>
                <button
                  type="button"
                  onClick={() => {
                    setShowCreateForm(false);
                    setEditingBooking(null);
                    setFormData({
                      service_type: 'consultation',
                      details: '',
                      scheduled_for: ''
                    });
                  }}
                  className="bg-gray-300 text-gray-700 px-6 py-2 rounded-lg hover:bg-gray-400"
                >
                  Cancel
                </button>
              </div>
            </form>
          </div>
        )}

        {/* Error Message */}
        {error && (
          <div className="bg-red-100 border border-red-400 text-red-700 px-4 py-3 rounded mb-4">
            {error}
          </div>
        )}

        {/* Bookings List */}
        {loading && !showCreateForm ? (
          <div className="text-center py-8">
            <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-blue-600 mx-auto"></div>
            <p className="mt-4 text-gray-600">Loading bookings...</p>
          </div>
        ) : bookings.length === 0 ? (
          <div className="bg-white rounded-lg shadow p-8 text-center">
            <Calendar size={48} className="mx-auto text-gray-400 mb-4" />
            <h3 className="text-lg font-semibold text-gray-900 mb-2">No Bookings Found</h3>
            <p className="text-gray-600 mb-4">
              {Object.values(filters).some(f => f) ? 'Try adjusting your filters.' : 'Create your first booking to get started.'}
            </p>
            {!Object.values(filters).some(f => f) && (
              <button
                onClick={() => setShowCreateForm(true)}
                className="bg-blue-600 text-white px-6 py-2 rounded-lg hover:bg-blue-700"
              >
                Create Booking
              </button>
            )}
          </div>
        ) : (
          <>
            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
              {bookings.map((booking) => {
                const ServiceIcon = serviceTypes.find(t => t.value === booking.service_type)?.icon || Calendar;
                return (
                  <div key={booking.id} className="bg-white rounded-lg shadow hover:shadow-lg transition-shadow">
                    <div className="p-6">
                      <div className="flex items-start justify-between mb-4">
                        <div className="flex items-center space-x-3">
                          <div className="p-2 bg-blue-100 rounded-lg">
                            <ServiceIcon size={20} className="text-blue-600" />
                          </div>
                          <div>
                            <h3 className="font-semibold text-gray-900 capitalize">{booking.service_type}</h3>
                            <span className={`inline-block px-2 py-1 text-xs rounded-full ${apiUtils.getBookingStatusColor(booking.status)}`}>
                              {booking.status}
                            </span>
                          </div>
                        </div>
                        <div className="flex space-x-2">
                          <button
                            onClick={() => handleEdit(booking)}
                            className="text-gray-600 hover:text-blue-600"
                          >
                            <Edit size={16} />
                          </button>
                          <button
                            onClick={() => handleDelete(booking.id)}
                            className="text-gray-600 hover:text-red-600"
                          >
                            <Trash2 size={16} />
                          </button>
                        </div>
                      </div>

                      <div className="space-y-2 text-sm text-gray-600">
                        <div className="flex items-center space-x-2">
                          <Clock size={16} />
                          <span>{apiUtils.parseBookingDate(booking.scheduled_for).toLocaleString()}</span>
                        </div>
                        {booking.details && (
                          <p className="text-gray-700">{booking.details}</p>
                        )}
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>

            {/* Pagination */}
            {pagination.pages > 1 && (
              <div className="flex justify-center mt-8">
                <div className="flex space-x-2">
                  <button
                    onClick={() => handlePageChange(pagination.page - 1)}
                    disabled={pagination.page === 1}
                    className="px-3 py-2 border border-gray-300 rounded-lg disabled:opacity-50 hover:bg-gray-50"
                  >
                    Previous
                  </button>
                  {Array.from({ length: pagination.pages }, (_, i) => i + 1).map(page => (
                    <button
                      key={page}
                      onClick={() => handlePageChange(page)}
                      className={`px-3 py-2 border rounded-lg ${
                        pagination.page === page
                          ? 'bg-blue-600 text-white border-blue-600'
                          : 'border-gray-300 hover:bg-gray-50'
                      }`}
                    >
                      {page}
                    </button>
                  ))}
                  <button
                    onClick={() => handlePageChange(pagination.page + 1)}
                    disabled={pagination.page === pagination.pages}
                    className="px-3 py-2 border border-gray-300 rounded-lg disabled:opacity-50 hover:bg-gray-50"
                  >
                    Next
                  </button>
                </div>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
};

export default Booking;