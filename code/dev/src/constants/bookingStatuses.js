export const BOOKING_STATUSES = {
  CONFIRMED: 'confirmed',
  PENDING: 'pending',
  COMPLETED: 'completed',
  CANCELLED: 'cancelled',
  RESCHEDULED: 'rescheduled'
};

export const STATUS_COLORS = {
  [BOOKING_STATUSES.CONFIRMED]: 'bg-green-100 text-green-800',
  [BOOKING_STATUSES.PENDING]: 'bg-yellow-100 text-yellow-800',
  [BOOKING_STATUSES.COMPLETED]: 'bg-blue-100 text-blue-800',
  [BOOKING_STATUSES.CANCELLED]: 'bg-red-100 text-red-800',
  [BOOKING_STATUSES.RESCHEDULED]: 'bg-purple-100 text-purple-800'
};