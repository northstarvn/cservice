```javascript
describe('Secure Journey E2E', () => {
  it('should handle secure journey', () => {
    // Mock JWT token
    cy.request('POST', '/api/login', { username: 'user123', password: 'Test123!' })
      .then((response) => {
        const token = response.body.token;
        cy.visit('/', { headers: { Authorization: `Bearer ${token}` } });
      });

    cy.get('#main_menu a[href="/chat"]').click();
    cy.get('#chat_input').type('Secure message');
    cy.get('#send_button').click();
    cy.get('.chat_history').should('contain', 'Please provide details');

    cy.get('#main_menu a[href="/booking"]').click();
    cy.get('#service_type').select('Delivery');
    cy.get('#booking_date').type('2025-10-10');
    cy.get('#booking_time').type('14:00');
    cy.get('#submit_booking').click();
    cy.get('#booking_confirmation_popup').should('be.visible');
  });
});
```