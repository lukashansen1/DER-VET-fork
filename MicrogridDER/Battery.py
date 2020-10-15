"""
BatteryTech.py

This Python class contains methods and attributes specific for technology analysis within StorageVet.
"""

__author__ = 'Halley Nathwani'
__copyright__ = 'Copyright 2018. Electric Power Research Institute (EPRI). All Rights Reserved.'
__credits__ = ['Miles Evans', 'Andres Cortes', 'Evan Giarta', 'Halley Nathwani']
__license__ = 'EPRI'
__maintainer__ = ['Halley Nathwani', 'Miles Evans']
__email__ = ['hnathwani@epri.com', 'mevans@epri.com']
__version__ = 'beta'  # beta version

import cvxpy as cvx
from storagevet.Technology import BatteryTech
from MicrogridDER.ESSSizing import ESSSizing
from ErrorHandelling import *
import pandas as pd
import numpy as np

DEBUG = False


class Battery(BatteryTech.Battery, ESSSizing):
    """ Battery class that inherits from Storage.

    """

    def __init__(self, params):
        """ Initializes a battery class that inherits from the technology class.
        It sets the type and physical constraints of the technology.

        Args:
            params (dict): params dictionary from dataframe for one case
        """
        TellUser.debug(f"Initializing {__name__}")
        super().__init__(params)  # BatteryTech.Battery->ESSizing->EnergyStorage->DER->Sizing
        self.user_duration = params['duration_max']
        self.state_of_health = params['state_of_health'] / 100
        self.years_system_degraded = set()
        self.yearly_degradation_report = pd.DataFrame()

        if self.user_duration:
            self.size_constraints += [cvx.NonPos(self.ene_max_rated - self.user_duration*self.dis_max_rated)]

    def initialize_degradation_module(self, opt_agg):
        """

        Notes: Should be called once, after optimization levels are assigned, but before
        optimization loop gets called

        Args:
            opt_agg (DataFrame):

        Returns: None

        """
        super(Battery, self).initialize_degradation_module(opt_agg)
        if self.incl_cycle_degrade:
            # calculate current degrade_perc since installation
            step_before_optimziation_problems = opt_agg.sort_index().index[0] - pd.Timedelta(self.dt, unit='h')
            self.calc_degradation('Optimization Start', self.operation_year.to_timestamp(), step_before_optimziation_problems)

    def calc_degradation(self, opt_period, start_dttm, last_dttm):
        """ calculate degradation percent based on yearly degradation and cycle degradation

        Args:
            opt_period: the index of the optimization that occurred before calling this function, None if
                no optimization problem has been solved yet
            start_dttm (DateTime): Start timestamp to calculate degradation. ie. the first datetime in the optimization
                problem
            last_dttm (DateTime): End timestamp to calculate degradation. ie. the last datetime in the optimization
                problem

        A percent that represented the energy capacity degradation
        """
        super(Battery, self).calc_degradation(opt_period, start_dttm, last_dttm)
        if self.incl_cycle_degrade:
            if self.degraded_energy_capacity() <= self.ene_max_rated * self.state_of_health:
                # record the year that the energy capacity reaches the point of replacement
                self.years_system_degraded.add(start_dttm.year)

                # reset the energy capacity to its original nameplate if replaceable
                if self.replaceable:
                    self.degrade_perc = 0
                    self.effective_soe_max = self.ulsoc * self.ene_max_rated
                    self.effective_soe_min = self.llsoc * self.ene_max_rated

    def set_end_of_life_based_on_degradation_cycle(self, analysis_years, start_year, end_year):
        """If degradation occurred AND it it the end of the optimization loop call this method -->
                if state of health reaches 0 during optimization loop --> calculate expected lifetime
                ELSE
                calculate the average yearly degradation, then estimate the expected lifetime is 1 / yearly degradation
            Reports to user if the expected lifetime is not the same as the user inputted expected lifetime

        Args:
            analysis_years (list):
            start_year (pd.Period):
            end_year (pd.Period):

        Returns: the new last year of operation for the BAT after 1 lifetime

        """
        if self.incl_cycle_degrade:
            # ESTIMATE EXPECTED LIFETIME
            num_full_lifetimes = len(self.years_system_degraded)
            if num_full_lifetimes:
                # get number of years it took to be replaced (get average if replaced more than once)
                foo = max(self.years_system_degraded) + 1 - self.operation_year.year
                avg_lifetime = foo / num_full_lifetimes
                # set FAILURE_YEARS to be the years that the system degraded
                self.failure_years = list(self.years_system_degraded)
            else:
                # create a data frame with a row for every year in the project lifetime
                yr_index = pd.period_range(start=start_year, end=end_year, freq='y')
                self.yearly_degradation_report = pd.Series(index=pd.Index(yr_index))

                # determine yearly degradation for the years that we counted cycles for
                no_years_solved = len(analysis_years)
                analysis_years = np.sort(analysis_years)  # sort the list of years, smallest to largest
                no_optimizations_per_year = len(self.degrade_data.iloc[1:].index) / no_years_solved
                for indx, year in enumerate(analysis_years):
                    first_degrad_inx = indx * no_optimizations_per_year
                    initial_degradation = self.degrade_data.iloc[int(first_degrad_inx)]['degradation']
                    last_degrad_idx = first_degrad_inx + no_optimizations_per_year
                    final_degradation = self.degrade_data.iloc[int(last_degrad_idx)]['degradation']
                    tot_yr_degradation = final_degradation - initial_degradation
                    self.yearly_degradation_report[pd.Period(year, freq='y')] = tot_yr_degradation
                # fill in the remaining years (assume constant degradation)
                self.yearly_degradation_report.fillna(method='ffill', inplace=True)
                # estimate lifetime with average yearly degradation
                avg_lifetime = (1-self.state_of_health)/self.yearly_degradation_report.mean()

            # report actual EOL to user
            TellUser.warning(f"{self.unique_tech_id()} degradation is ON, and so we have estimated the EXPECTED_LIFETIME" +
                             f" to be {int(avg_lifetime)}  (inputted value: {self.expected_lifetime})")
            # set EXPECTED_LIFETIME to be the actual EOL -- it should never be 0 years
            self.expected_lifetime = max(int(avg_lifetime), 1)

            # set FAILURE_YEARS to be the years that the system degraded to SOH=0
            failed_on = max(self.years_system_degraded) if num_full_lifetimes else None
            self.set_failure_years(end_year, fail_on=failed_on)
            return self.operation_year + self.expected_lifetime-1

    def constraints(self, mask, **kwargs):
        """ Builds the master constraint list for the subset of timeseries data being optimized.

        Args:
            mask (DataFrame): A boolean array that is true for indices corresponding to time_series data included
                in the subs data set

        Returns:
            A list of constraints that corresponds the battery's physical constraints and its service constraints
        """

        constraint_list = super().constraints(mask, **kwargs)  # BatteryTech.Battery->ESSSizing->EnergyStorage
        return constraint_list

    def drill_down_reports(self, monthly_data=None, time_series_data=None, technology_summary=None, sizing_df=None):
        """Calculates any service related dataframe that is reported to the user.

        Args:
            monthly_data:
            time_series_data:
            technology_summary:
            sizing_df:



        Returns: dictionary of DataFrames of any reports that are value stream specific
            keys are the file name that the df will be saved with
        """

        dct = super().drill_down_reports(monthly_data, time_series_data, technology_summary, sizing_df)
        self.yearly_degradation_report.name = "Yearly Degradation"
        dct[f"{self.name.replace(' ', '_')}_yearly_degradation"] = self.yearly_degradation_report
        return dct
