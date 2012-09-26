module qutraj_precision
  ! Module for setting working precision

  implicit none

  integer, parameter :: sp = kind(1.0e0) ! working precision
  integer, parameter :: dp = kind(1.0d0) ! working precision
  !integer, parameter :: sp = selected_real_kind(6) ! single precision
  !integer, parameter :: dp = selected_real_kind(15) ! double precision
  integer, parameter :: wp = dp ! working precision
  integer, parameter :: wpc = wp ! working precision complex numbers
end module