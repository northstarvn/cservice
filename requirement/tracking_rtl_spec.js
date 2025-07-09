describe('Multilingual and Tracking E2E', () => {
  it('should handle RTL and tracking', () => {
    cy.visit('/profile');
    cy.get('#preferred_language').select('ar');
    cy.get('html').should('have.attr', 'dir', 'rtl');
    cy.get('#main_menu a[href="/chat"]').click();
    cy.get('#chat_input').type('تتبع الشحنة');
    cy.get('#send_button').click();
    cy.get('.chat_history').should('contain', 'يرجى تقديم رقم التتبع');
    cy.get('#main_menu a[href="/tracking"]').click();
    cy.get('#tracking_id').type('track_123');
    cy.get('#track_button').click();
    cy.get('#tracking_result_popup').should('contain', 'In Transit');
  });
});